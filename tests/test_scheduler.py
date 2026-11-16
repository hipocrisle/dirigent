import asyncio
import time

import pytest
import yaml

from src.db import DB
from src.models import OrderSpec, StepResult, ErrorClass
from src.scheduler import Scheduler
from src.states import OrderState, StepState
from tests.conftest import run_ticks, yaml_order


async def submit(db: DB, text: str) -> str:
    spec = OrderSpec.model_validate(yaml.safe_load(text))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


async def step_state(db: DB, oid: str, sid: str) -> StepState:
    for r in await db.steps_of(oid):
        if r["id"] == sid:
            return StepState(r["state"])
    raise KeyError(sid)


async def test_chain_e2e(db, sched):
    """s1 -> s2: простая цепочка до done."""
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.ok"},
        {"id": "s2", "type": "mock.ok", "depends_on": ["s1"]},
    ]))
    await run_ticks(sched, 6)
    assert await db.get_order_state(oid) == OrderState.done
    assert (await db.order_progress(oid))["by_state"] == {"completed": 2}


async def test_retry_then_success(db, sched):
    """s1 падает runtime, второй мок-прогон ок: scripted executor."""
    scripted = {"s1": StepResult(ok=False, error_class=ErrorClass.runtime, error_text="boom")}
    calls = {"n": 0}
    orig = scripted  # noqa

    from src.executors.mock import ScriptedExecutor

    class Flaky(ScriptedExecutor):
        async def run(self, step, workdir=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return StepResult(ok=False, error_class=ErrorClass.runtime, error_text="boom")
            return StepResult(ok=True)

    sched.exec["mock"] = Flaky()
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.ok",
         "retry": {"max_attempts": 2, "backoff_seconds": 0.1}},
    ]))
    await run_ticks(sched, 8)
    assert await db.get_order_state(oid) == OrderState.done
    assert calls["n"] == 2


async def test_quota_waits_and_resumes(db, sched):
    """mock-исполнитель: первый запуск — квота (без reset_at), потом ок."""
    from src.executors.mock import ScriptedExecutor

    class QuotaOnce(ScriptedExecutor):
        async def run(self, step, workdir=None):
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls == 1:
                return StepResult(ok=False, error_class=ErrorClass.quota,
                                  error_text="quota", quota_reset_at=None)
            return StepResult(ok=True)

    sched.exec["mock"] = QuotaOnce()
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.quota"},
    ]))
    seen_quota = False
    for _ in range(10):
        await sched.tick()
        await asyncio.sleep(0.05)
        cur = await db.conn.execute(
            "SELECT 1 FROM events WHERE order_id=? AND kind='step.transition'"
            " AND data_json LIKE '%waiting_quota%' LIMIT 1", (oid,))
        if await cur.fetchone():
            seen_quota = True
            break
    assert seen_quota, "шаг не прошёл через waiting_quota"
    await run_ticks(sched, 6)
    assert await db.get_order_state(oid) == OrderState.done


async def test_quota_with_reset_at_not_polled_early(db, sched):
    """reset_at в будущем: исполнитель НЕ дёргается (нет бессмысленного polling)."""
    from src.executors.mock import ScriptedExecutor
    probed = {"n": 0}

    class Counting(ScriptedExecutor):
        async def available(self):
            probed["n"] += 1
            return True

        async def run(self, step, workdir=None):
            return StepResult(ok=False, error_class=ErrorClass.quota,
                              error_text="quota", quota_reset_at=time.time() + 3600)

    sched.exec["mock"] = Counting()
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.quota"},
    ]))
    n0 = probed["n"]
    await run_ticks(sched, 6)
    assert await step_state(db, oid, "s1") == StepState.waiting_quota
    # available() не дёргался после ухода в waiting_quota (знаем reset_at)
    assert probed["n"] <= n0 + 1


async def test_max_attempts_escalates(db, sched):
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.fail runtime",
         "retry": {"max_attempts": 2, "backoff_seconds": 0.1}},
    ]))
    await run_ticks(sched, 10)
    assert await step_state(db, oid, "s1") == StepState.failed
    prog = await db.order_progress(oid)
    assert prog["verdict"] == "has_failed"
    assert await db.get_order_state(oid) == OrderState.error


async def test_approval_after(db, sched):
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.ok",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 60}},
    ]))
    await run_ticks(sched, 5)
    assert await step_state(db, oid, "s1") == StepState.waiting_approval
    cur = await db.conn.execute("SELECT id FROM approvals WHERE order_id=? AND status='open'", (oid,))
    aid = (await cur.fetchone())["id"]
    await sched.approve(aid)
    await sched.tick()
    assert await db.get_order_state(oid) == OrderState.done


async def test_approval_reject_cancels(db, sched):
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.ok",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 60}},
    ]))
    await run_ticks(sched, 5)
    cur = await db.conn.execute("SELECT id FROM approvals WHERE order_id=? AND status='open'", (oid,))
    aid = (await cur.fetchone())["id"]
    await sched.reject(aid)
    await sched.tick()
    assert await step_state(db, oid, "s1") == StepState.cancelled
    assert await db.get_order_state(oid) == OrderState.error


async def test_dep_failure_cascades(db, sched):
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.fail runtime",
         "retry": {"max_attempts": 1, "backoff_seconds": 0.1}},
        {"id": "s2", "type": "mock.ok", "depends_on": ["s1"]},
    ]))
    await run_ticks(sched, 8)
    assert await step_state(db, oid, "s1") == StepState.failed
    assert await step_state(db, oid, "s2") == StepState.cancelled


async def test_crash_recovery(db):
    """Демон умер посреди running: новый scheduler recover() -> шаг снова в очереди."""
    oid = await submit(db, yaml_order([
        {"id": "s1", "type": "mock.ok",
         "retry": {"max_attempts": 2, "backoff_seconds": 0.1}},
    ]))
    # имитация: шаг захвачен lease и демон исчез
    await db.promote_pending()
    assert await db.lease_step(oid, "s1", "sched-dead", lease_seconds=0.2)
    await asyncio.sleep(0.3)  # lease протух

    sched2 = Scheduler(db)
    await sched2.recover()
    assert await step_state(db, oid, "s1") == StepState.ready

    await run_ticks(sched2, 8, delay=0.2)  # backoff 1.0s после recovery
    assert await db.get_order_state(oid) == OrderState.done


async def test_parallel_cap(db, sched):
    """max_parallel=2: длинные шаги не стартуют третьим."""
    oid = await submit(db, yaml_order([
        {"id": "a", "type": "mock.ok 0.4"},
        {"id": "b", "type": "mock.ok 0.4"},
        {"id": "c", "type": "mock.ok 0.4"},
    ]))
    await sched.tick()
    await asyncio.sleep(0.05)
    running = 0
    for sid in ("a", "b", "c"):
        if await step_state(db, oid, sid) == StepState.running:
            running += 1
    assert running == 2
    await run_ticks(sched, 10, delay=0.15)
    assert await db.get_order_state(oid) == OrderState.done
