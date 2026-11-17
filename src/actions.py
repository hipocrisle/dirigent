"""Действия пользователя над заказами: апрувы, пауза, отмена.

Используются CLI, HTTP API и (через API) Telegram-бот. Вся логика —
на уровне БД, без планировщика: демон подхватывает новые состояния в tick().
"""
from __future__ import annotations

import time

from .db import DB
from .states import OrderState, StepState


async def _claim_approval(db: DB, aid: str, status: str) -> dict | None:
    """Атомарно забирает открытый апрув (claim) -> строка | None.

    Ровно один претендент переводит status open -> approved/rejected; второй
    клик (или параллельный /approve из бота/CLI) получает None и не применяет
    решение повторно (инцидент «двойное approve/reject» 17.11).
    """
    cur = await db.conn.execute(
        "UPDATE approvals SET status=?, resolved=? WHERE id=? AND status='open'",
        (status, time.time(), aid))
    await db.conn.commit()
    if cur.rowcount != 1:
        return None
    cur = await db.conn.execute("SELECT * FROM approvals WHERE id=?", (aid,))
    return await cur.fetchone()


async def approve(db: DB, aid: str) -> bool:
    row = await _claim_approval(db, aid, "approved")
    if not row:
        return False
    oid, sid = row["order_id"], row["step_id"]
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
    row = await _claim_approval(db, aid, "rejected")
    if not row:
        return False
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
    """Пауза: running шаги дорабатывают до конца, новые не запускаются.

    В Telegram НЕ уходит: это обычный переход, видимый в UI/аудите (events).
    """
    ok = await db.transition_order(oid, OrderState.paused)
    if ok:
        await db.event("order.paused", oid)
    return ok


async def resume_order(db: DB, oid: str) -> bool:
    """Продолжение после паузы. Тоже UI/аудит, без Telegram (P4 quiet)."""
    ok = await db.transition_order(oid, OrderState.active)
    if ok:
        await db.event("order.resumed", oid)
    return ok


async def retry_step(db: DB, oid: str, sid: str) -> bool:
    """Ручной перезапуск упавшего шага.

    attempts = монотонный счётчик ФАКТИЧЕСКИХ запусков (инкремент в lease_step,
    атомарно с захватом). Ручной разрешён, пока attempts < MAX_STEP_LAUNCHES (3);
    счётчик НИКОГДА не сбрасывается — ни retry, ни рестарт демона.
    """
    from .policy import MAX_STEP_LAUNCHES
    cur = await db.conn.execute(
        "SELECT attempts, state FROM steps WHERE order_id=? AND id=?", (oid, sid))
    row = await cur.fetchone()
    if not row:
        return False
    from .states import StepState
    if StepState(row["state"]) != StepState.failed:
        return False
    if row["attempts"] >= MAX_STEP_LAUNCHES:
        await db.event("step.retry_refused", oid, sid,
                       {"attempts": row["attempts"], "cap": MAX_STEP_LAUNCHES})
        from . import notify
        notify.enqueue("step.retry_refused",
                       notify.KIND_TITLES["step.retry_refused"],
                       f"Заказ: <code>{oid}</code>\nШаг: <code>{sid}</code>\n"
                       f"Запусков: {row['attempts']}/{MAX_STEP_LAUNCHES}",
                       dedup_key=f"step:{oid}/{sid}:refused", order_id=oid, step_id=sid)
        return False
    ok = await db.transition_step(
        oid, sid, StepState.retry_scheduled, force=True,
        backoff_until=__import__("time").time() + 1.0,
        error_text=None)
    if ok:
        await db.event("step.retry_manual", oid, sid,
                       {"attempts": row["attempts"],
                        "next_launch": row["attempts"] + 1})
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
