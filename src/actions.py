"""Действия пользователя над заказами: апрувы, пауза, отмена.

Используются CLI, HTTP API и (через API) Telegram-бот. Вся логика —
на уровне БД, без планировщика: демон подхватывает новые состояния в tick().
"""
from __future__ import annotations

from .db import DB
from .states import OrderState, StepState


async def approve(db: DB, aid: str) -> bool:
    cur = await db.conn.execute("SELECT * FROM approvals WHERE id=?", (aid,))
    row = await cur.fetchone()
    if not row or row["status"] != "open":
        return False
    oid, sid = row["order_id"], row["step_id"]
    await db.resolve_approval(aid, "approved")
    spec = await db.get_step_spec(oid, sid)
    cur2 = await db.conn.execute(
        "SELECT state FROM steps WHERE order_id=? AND id=?", (oid, sid))
    st = StepState((await cur2.fetchone())["state"])
    if st == StepState.waiting_approval and spec.approval.mode == "before":
        ok = await db.transition_step(oid, sid, StepState.ready)
    elif st == StepState.waiting_approval:
        ok = await db.transition_step(oid, sid, StepState.completed)
    else:
        ok = False
    await db.event("approval.approved", oid, sid, {"approval": aid, "applied": ok})
    return ok


async def reject(db: DB, aid: str) -> bool:
    cur = await db.conn.execute("SELECT * FROM approvals WHERE id=?", (aid,))
    row = await cur.fetchone()
    if not row or row["status"] != "open":
        return False
    await db.resolve_approval(row["id"], "rejected")
    ok = await db.transition_step(row["order_id"], row["step_id"], StepState.cancelled,
                                  error_text="rejected by human")
    await db.event("approval.rejected", row["order_id"], row["step_id"],
                   {"approval": aid, "applied": ok})
    return ok


async def cancel_order(db: DB, oid: str) -> bool:
    ok = await db.transition_order(oid, OrderState.cancelled)
    for s in await db.steps_of(oid):
        st = StepState(s["state"])
        if st not in (StepState.completed, StepState.failed, StepState.cancelled):
            await db.transition_step(oid, s["id"], StepState.cancelled)
    return ok


async def pause_order(db: DB, oid: str) -> bool:
    """Пауза: running шаги дорабатывают до конца, новые не запускаются."""
    ok = await db.transition_order(oid, OrderState.paused)
    if ok:
        await db.event("order.paused", oid)
    return ok


async def resume_order(db: DB, oid: str) -> bool:
    ok = await db.transition_order(oid, OrderState.active)
    if ok:
        await db.event("order.resumed", oid)
    return ok


async def retry_step(db: DB, oid: str, sid: str) -> bool:
    """Ручной перезапуск упавшего шага. Потолок запусков MAX_STEP_LAUNCHES."""
    from .policy import MAX_STEP_LAUNCHES
    cur = await db.conn.execute(
        "SELECT attempts, max_attempts, state FROM steps WHERE order_id=? AND id=?",
        (oid, sid))
    row = await cur.fetchone()
    if not row:
        return False
    from .states import StepState
    if StepState(row["state"]) != StepState.failed:
        return False
    if row["attempts"] >= MAX_STEP_LAUNCHES:
        await db.event("step.retry_refused", oid, sid,
                       {"attempts": row["attempts"]})
        return False
    # оставляем одну попытку в запасе (max_attempts=2: attempts=1 -> ещё один запуск)
    await db.conn.execute(
        "UPDATE steps SET attempts=? WHERE order_id=? AND id=?",
        (min(row["attempts"], row["max_attempts"] - 1), oid, sid))
    await db.conn.commit()
    ok = await db.transition_step(
        oid, sid, StepState.retry_scheduled, force=True,
        backoff_until=__import__("time").time() + 1.0,
        error_text=None)
    if ok:
        await db.event("step.retry_manual", oid, sid, {"attempts": row["attempts"]})
        ostate = await db.get_order_state(oid)
        if ostate == OrderState.error:
            await db.transition_order(oid, OrderState.active)
    return ok


async def stats(db: DB) -> dict:
    """Метрики шапки (только факт; что неизмеримо — unknown, не выдумываем)."""
    async def one(q: str, *a):
        cur = await db.conn.execute(q, a)
        return (await cur.fetchone())[0]

    running = await one("SELECT COUNT(*) FROM steps WHERE state='running'")
    ready = await one("SELECT COUNT(*) FROM steps WHERE state='ready'")
    waiting = await one("SELECT COUNT(*) FROM steps WHERE state IN ('waiting_quota','waiting_resource','waiting_approval')")
    # расход: сумма usage.usd из result_json завершённых шагов
    cur = await db.conn.execute(
        "SELECT result_json FROM steps WHERE state='completed' AND result_json IS NOT NULL")
    usd_total = 0.0
    tokens_total = 0.0
    n_usd = 0
    for r in await cur.fetchall():
        import json
        try:
            usage = json.loads(r["result_json"]).get("usage") or {}
        except Exception:
            continue
        if usage.get("usd"):
            usd_total += usage["usd"]
            n_usd += 1
        tokens_total += usage.get("tokens_in", 0) + usage.get("tokens_out", 0)
    done_orders = await one("SELECT COUNT(*) FROM orders WHERE state='done'")
    return {
        "agents_active": running,          # факт
        "tasks_running": running,
        "tasks_ready": ready,
        "tasks_waiting": waiting,
        "spent_usd": round(usd_total, 6) if n_usd else "unknown",  # факт, иначе честный unknown
        "tokens_total": int(tokens_total) if tokens_total else "unknown",
        "orders_done": done_orders,
        "codex_quota": "unknown",          # tier-2 probe (WP-8) ещё не подключён
    }
