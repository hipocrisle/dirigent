"""Планировщик: цикл продвижения, lease, квота, ретраи, апрувы, восстановление.

Ядро не вызывает LLM никогда (AD-1). Brain подключается отдельно для создания
плана из свободной формулировки; здесь — только исполнение DAG.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from .db import DB
from .executors import registry
from .models import StepResult
from .states import OrderState, StepState

log = logging.getLogger("dirigent.scheduler")

LEASE_SECONDS = 600.0
HEARTBEAT_SECONDS = 30.0
QUOTA_RECHECK_SECONDS = 60.0


class Scheduler:
    def __init__(self, db: DB, max_parallel: int = 2,
                 executor_override: dict[str, object] | None = None):
        self.db = db
        self.max_parallel = max_parallel
        self.slots = asyncio.Semaphore(max_parallel)
        self.owner = f"sched-{uuid.uuid4().hex[:8]}"
        self._tasks: set[asyncio.Task] = set()
        self._stopped = asyncio.Event()
        self.exec = dict(registry)
        if executor_override:
            self.exec.update(executor_override)

    # ── жизненный цикл ────────────────────────────────────────
    async def run_forever(self, tick: float = 1.0) -> None:
        await self.recover()
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(tick)
        await self.shutdown()

    async def stop(self) -> None:
        self._stopped.set()

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def recover(self) -> None:
        """После перезапуска: вернуть зависшие шаги в очередь (спека §6.4)."""
        await self.db.requeue_orphaned()
        requeued = await self.db.requeue_stale()
        for oid, sid, what in requeued:
            await self.db.event("step.recovered", oid, sid, {"action": what})
        log.info("recovery done: %s", requeued)

    # ── один проход ───────────────────────────────────────────
    async def tick(self) -> None:
        await self.db.promote_pending()
        for r in await self.db.due_retries():
            await self.db.transition_step(r["order_id"], r["id"], StepState.ready,
                                          backoff_until=None)
        await self._expire_approvals()
        await self._recheck_quota()
        await self._reap_stale_leases()
        await self._launch_ready()
        await self._update_orders()

    async def _launch_ready(self) -> None:
        free = self.max_parallel - len(self._tasks)
        if free <= 0:
            return
        for row in await self.db.ready_steps(limit=free):
            if self._stopped.is_set():
                return
            ok = await self.db.lease_step(row["order_id"], row["id"],
                                          self.owner, LEASE_SECONDS)
            if not ok:
                continue
            t = asyncio.create_task(self._run(row["order_id"], row["id"]))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _run(self, oid: str, sid: str) -> None:
        async with self.slots:
            try:
                await self._run_inner(oid, sid)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.exception("run %s/%s failed", oid, sid)
                await self.db.transition_step(
                    oid, sid, StepState.failed, error_class="unknown",
                    error_text=f"scheduler: {e!r}", lease_owner=None, lease_until=None)

    async def _run_inner(self, oid: str, sid: str) -> None:
        spec = await self.db.get_step_spec(oid, sid)
        tag = spec.type.split(".")[0]
        ex = self.exec.get(tag)
        if ex is None:
            await self.db.transition_step(
                oid, sid, StepState.failed, error_class="unknown",
                error_text=f"no executor for tag {tag}", lease_owner=None, lease_until=None)
            return

        if not await ex.available():
            await self.db.transition_step(
                oid, sid, StepState.waiting_resource, lease_owner=None, lease_until=None)
            await self.db.event("step.waiting_resource", oid, sid, {"tag": tag})
            return

        if spec.approval.mode == "before":
            if not await self._ask_and_wait(oid, sid,
                                            f"Шаг {sid} требует подтверждения перед запуском: {spec.prompt}",
                                            spec):
                return

        hb = asyncio.create_task(self._heartbeat(oid, sid))
        try:
            result = await asyncio.wait_for(
                ex.run(spec, workdir=None), timeout=spec.timeout_seconds)
        except asyncio.TimeoutError:
            result = StepResult(ok=False, error_class="timeout",
                                error_text=f"timeout {spec.timeout_seconds}s")
        finally:
            hb.cancel()

        if result.ok and spec.acceptance.get("checks"):
            from .checkers import run_acceptance
            chk = run_acceptance(spec, None, result.artifacts)
            if not chk.ok:
                result = StepResult(ok=False, error_class="runtime",
                                    error_text=f"acceptance failed: {chk.details}")
        if result.ok and spec.approval.mode == "after":
            await self.db.transition_step(oid, sid, StepState.waiting_approval,
                                          lease_owner=None, lease_until=None,
                                          result_json=result.model_dump_json())
            await self._open_approval(
                oid, sid,
                f"Шаг {sid} выполнен. Принять результат? ({list(result.artifacts)})",
                spec)
            return
        await self.db.finish_step(oid, sid, result)

    async def _heartbeat(self, oid: str, sid: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.db.extend_lease(oid, sid, self.owner, LEASE_SECONDS)

    # ── апрувы (неблокирующие) ────────────────────────────────
    async def _open_approval(self, oid: str, sid: str, question: str, spec) -> None:
        aid = await self.db.create_approval(
            oid, sid, question, spec.approval.default_action,
            spec.approval.timeout_seconds)
        await self.db.event("approval.asked", oid, sid,
                            {"approval": aid, "default": spec.approval.default_action})

    async def _ask_and_wait(self, oid: str, sid: str, question: str, spec) -> bool:
        """approval.mode=before: шаг уже в running; при отказе — cancelled."""
        await self._open_approval(oid, sid, question, spec)
        # переводим в waiting_approval и освобождаем lease — ждём решения
        await self.db.transition_step(oid, sid, StepState.waiting_approval,
                                      lease_owner=None, lease_until=None)
        return True  # решение придёт через approve()/reject()

    async def approve(self, aid: str) -> None:
        cur = await self._approval_row(aid)
        if not cur:
            return
        oid, sid = cur["order_id"], cur["step_id"]
        await self.db.resolve_approval(aid, "approved")
        spec = await self.db.get_step_spec(oid, sid)
        state = (await self.db.steps_of(oid))
        st = {r["id"]: StepState(r["state"]) for r in state}[sid]
        if st == StepState.waiting_approval and spec.approval.mode == "before":
            await self.db.transition_step(oid, sid, StepState.ready)  # уйдёт в работу
        elif st == StepState.waiting_approval:
            await self.db.transition_step(oid, sid, StepState.completed)
        await self.db.event("approval.approved", oid, sid, {"approval": aid})

    async def reject(self, aid: str) -> None:
        cur = await self._approval_row(aid)
        if not cur:
            return
        oid, sid = cur["order_id"], cur["step_id"]
        await self.db.resolve_approval(aid, "rejected")
        await self.db.transition_step(oid, sid, StepState.cancelled,
                                      error_text="rejected by human")
        await self.db.event("approval.rejected", oid, sid, {"approval": aid})

    async def _approval_row(self, aid: str):
        # лёгкий доступ без расширения DB API
        cur = await self.db.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (aid,))
        return await cur.fetchone()

    async def _expire_approvals(self) -> None:
        for r in await self.db.expired_approvals():
            if r["default_action"] == "reject":
                await self.db.resolve_approval(r["id"], "timeout")
                await self.db.transition_step(
                    r["order_id"], r["step_id"], StepState.cancelled,
                    error_text="approval timeout (default=reject)")
            else:
                await self.db.resolve_approval(r["id"], "timeout")
                # default=wait: продлеваем молча ещё на таймаут
                spec = await self.db.get_step_spec(r["order_id"], r["step_id"])
                await self.db.conn.execute(
                    "UPDATE approvals SET status='open', expires_at=? WHERE id=?",
                    (__import__("time").time() + spec.approval.timeout_seconds, r["id"]))
                await self.db.conn.commit()

    # ── квота ─────────────────────────────────────────────────
    async def _recheck_quota(self) -> None:
        for r in await self.db.waiting_quota_steps():
            tag = r["type"].split(".")[0]
            ex = self.exec.get(tag)
            if ex is None:
                continue
            reset_at = r["quota_reset_at"]
            if reset_at is not None and reset_at > 0:
                import time
                if time.time() < reset_at:
                    continue  # знаем время — не дёргаем исполнитель
            try:
                if await ex.available():
                    await self.db.transition_step(r["order_id"], r["id"], StepState.ready,
                                                  quota_reset_at=None)
                    await self.db.event("step.quota_restored", r["order_id"], r["id"])
            except Exception:
                log.exception("quota recheck failed for %s/%s", r["order_id"], r["id"])

    async def _reap_stale_leases(self) -> None:
        for oid, sid, what in await self.db.requeue_stale():
            await self.db.event("step.lease_lost", oid, sid, {"action": what})

    # ── статусы заказов ───────────────────────────────────────
    async def _update_orders(self) -> None:
        cur = await self.db.conn.execute("SELECT id FROM orders WHERE state IN ('new','active','waiting_human')")
        for r in await cur.fetchall():
            oid = r["id"]
            prog = await self.db.order_progress(oid)
            ostate = await self.db.get_order_state(oid)
            if ostate == OrderState.new:
                await self.db.transition_order(oid, OrderState.active)
                ostate = OrderState.active
            if prog["verdict"] == "all_done":
                await self.db.transition_order(oid, OrderState.done)
                await self.db.event("order.done", oid)
            elif prog["verdict"] in ("has_failed", "has_cancelled"):
                await self.db.transition_order(oid, OrderState.error)
                await self.db.event("order.error", oid, data=prog)
            else:
                cur2 = await self.db.conn.execute(
                    "SELECT COUNT(*) n FROM approvals WHERE order_id=? AND status='open'",
                    (oid,))
                if (await cur2.fetchone())["n"] > 0:
                    await self.db.transition_order(oid, OrderState.waiting_human)
