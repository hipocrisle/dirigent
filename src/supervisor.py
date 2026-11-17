"""E2: локальный LLM-supervisor — управленческие решения без ожидания ChatGPT.

Демон раз в INTERVAL (и по событию) собирает СНИМОК (error-заказы, failed-шаги,
зависшие) и отдаёт ДЕШЁВОЙ модели (deepseek-chat) строгий шаблон: выбрать ОДНО
действие из вайтлиста или none. Решение валидируется, исполняется через actions.*,
пишется в аудит. Политика жёсткая: никаких бюджетных/опасных действий,
не более MAX_INTERVENTIONS_PER_ORDER вмешательств, dry-run по умолчанию.

Не supervisor заменяет ChatGPT — он закрывает часы, когда руководитель недоступен.
"""
from __future__ import annotations

import json
import logging
import time

from .db import DB
from .states import OrderState, StepState

log = logging.getLogger("dirigent.supervisor")

INTERVAL = 180.0
MAX_INTERVENTIONS_PER_ORDER = 2
MODE = "dry"  # dry | enforce (env DIRIGENT_SUPERVISOR_MODE)

PROMPT = """Ты — локальный supervisor оркестратора Dirigent. Реши ОДНО действие.

СНИМОК (JSON):
{snapshot}

ПОЛИТИКА (жёстко):
- действия только из: retry_step, cancel_order, none
- retry_step только для шага в failed с attempts < 3
- cancel_order только если у заказа ВСЕ шаги терминальны и есть failed/cancelled
- не более 2 вмешательств на заказ (счётчик в снимке)
- если сомневаешься — none
- НИКАКИХ других действий, никакого текста вне JSON

ОТВЕТ строго одной строкой JSON:
{{"action":"none"}} или {{"action":"retry_step","order_id":"...","step_id":"...","reason":"кратко"}} или {{"action":"cancel_order","order_id":"...","reason":"кратко"}}
"""


async def collect_snapshot(db: DB) -> dict:
    cur = await db.conn.execute(
        "SELECT id, state FROM orders WHERE state IN ('error','active')")
    orders = {r["id"]: r["state"] for r in await cur.fetchall()}
    snap: dict = {"orders": [], "interventions": {}}
    for oid, ostate in orders.items():
        steps = []
        for s in await db.steps_of(oid):
            if StepState(s["state"]) in (StepState.failed, StepState.running,
                                         StepState.waiting_resource):
                steps.append({"id": s["id"], "state": s["state"],
                              "attempts": s["attempts"],
                              "err": (s["error_text"] or "")[:120]})
        n = 0
        cur2 = await db.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE order_id=? AND kind='supervisor.action'",
            (oid,))
        n = (await cur2.fetchone())["n"]
        snap["orders"].append({"order_id": oid, "state": ostate, "steps": steps,
                               "interventions_done": n})
    return snap


def parse_decision(text: str) -> dict | None:
    try:
        line = next(l for l in (text or "").splitlines() if l.strip().startswith("{"))
        d = json.loads(line.strip())
    except Exception:
        return None
    if d.get("action") not in ("retry_step", "cancel_order", "none"):
        return None
    if d["action"] == "none":
        return {"action": "none"}
    if not isinstance(d.get("order_id"), str) or not d["order_id"]:
        return None
    if d["action"] == "retry_step" and not isinstance(d.get("step_id"), str):
        return None
    d["reason"] = str(d.get("reason", ""))[:200]
    return d


async def run_once(db: DB, sched, ask) -> dict:
    """Один цикл. ask(prompt)->str — вызов дешёвой модели (opencode executor)."""
    snap = await collect_snapshot(db)
    if not any(o["steps"] for o in snap["orders"]):
        return {"skipped": "nothing to decide"}
    payload = json.dumps(snap, ensure_ascii=False)[:3500]
    raw = await ask(PROMPT.replace("{snapshot}", payload))
    decision = parse_decision(raw or "")
    if decision is None:
        await db.event("supervisor.invalid", None, data={"raw": (raw or "")[:200]})
        return {"skipped": "unparseable"}
    if decision["action"] == "none":
        return {"decision": "none"}
    oid = decision["order_id"]
    order = next((o for o in snap["orders"] if o["order_id"] == oid), None)
    if order is None or order["interventions_done"] >= MAX_INTERVENTIONS_PER_ORDER:
        return {"skipped": "policy: intervention limit"}
    if decision["action"] == "retry_step":
        step = next((s for s in order["steps"]
                     if s["id"] == decision["step_id"] and s["state"] == "failed"
                     and s["attempts"] < 3), None)
        if step is None:
            return {"skipped": "policy: step not retryable"}
        if MODE == "enforce":
            from . import actions
            ok = await actions.retry_step(db, oid, decision["step_id"])
        else:
            ok = "dry-run"
    else:  # cancel_order
        if order["state"] != "error":
            return {"skipped": "policy: cancel only error orders"}
        if MODE == "enforce":
            from . import actions
            ok = await actions.cancel_order(db, oid)
        else:
            ok = "dry-run"
    await db.event("supervisor.action", oid,
                   data={"decision": decision, "mode": MODE, "result": str(ok)})
    return {"decision": decision, "mode": MODE, "result": str(ok)}


async def loop(db: DB, sched, ask, interval: float = INTERVAL,
               stop=None) -> None:
    import asyncio
    while stop is None or not stop.is_set():
        try:
            outcome = await run_once(db, sched, ask)
            if "decision" in outcome:
                log.info("supervisor: %s", outcome)
        except Exception:
            log.exception("supervisor cycle failed")
        await asyncio.sleep(interval)


def make_ask(exec_override=None):
    """ask() через opencode-адаптер (deepseek, копейки за вызов)."""
    from .executors import registry as exreg
    from .models import StepSpec
    ex = (exec_override or exreg).get("opencode")

    async def ask(prompt: str) -> str:
        spec = StepSpec(id="supervisor", type="opencode", prompt=prompt,
                        timeout_seconds=120)
        r = await ex.run(spec, workdir=None)
        return r.log_tail if r.ok else ""

    return ask
