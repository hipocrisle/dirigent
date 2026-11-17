"""Интеграционные регрессии: планировщик + outbox (тихий прод P4).

Сквозной путь: tick() -> notify.enqueue -> outbox -> (фейковый) TG.
Журнал заказов — настоящий (temp DB), outbox — изолированный.

«Реальные» заказы здесь идут на адаптере `opencode` (подменённый исполнением
в памяти), а «тестовые» — на `mock.*`/bench-pilot: именно по ТИПУ адаптера,
репозиторию и явным метаданным, как в проде.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from src.models import OrderSpec, StepResult
from src.scheduler import Scheduler
from src.states import OrderState
from tests.conftest import yaml_order


class OutboxBinding:
    """Переключает src.notify на журнал теста + чистая таблица outbox."""

    def __init__(self, tmp_path, monkeypatch):
        self.m = importlib.import_module("src.notify")
        importlib.reload(self.m)
        monkeypatch.setattr(self.m, "MODE", "quiet", raising=False)
        monkeypatch.setattr(self.m, "_allow_nonproduction_send", True, raising=False)
        self._mp = monkeypatch

    def bind(self, db_path: str):
        self._mp.setattr(self.m, "JOURNAL_DB", db_path)
        self.m._conn().close()   # создаёт outbox в том же файле журнала
        return self.m


class FakeExecutor:
    """Исполнитель, который отдаёт заданный результат мгновенно."""

    def __init__(self, tag: str, ok: bool = True, artifacts: dict | None = None):
        self.tag = tag
        self.ok = ok
        self.artifacts = artifacts or {}

    async def available(self):
        return True

    async def status(self, run_id):
        return None

    async def cancel(self, run_id):
        return None

    async def run(self, spec, workdir=None):
        if self.ok:
            return StepResult(ok=True, artifacts=dict(self.artifacts))
        return StepResult(ok=False, error_class="runtime", error_text="fake failure")


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    return OutboxBinding(tmp_path, monkeypatch)


async def submit(db, steps, *, goal="Реальная задача", repo=None,
                 constraints=None):
    import yaml
    spec = OrderSpec.model_validate(yaml.safe_load(
        yaml_order(steps, goal=goal, repo=repo, constraints=constraints or {})))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


def wait_steps(sched: Scheduler):
    """Дождаться фоновых задач шагов (они создаются в tick())."""
    return sched.shutdown()


async def run(sched: Scheduler, n: int = 8):
    import asyncio
    for _ in range(n):
        await sched.tick()
        await asyncio.sleep(0.05)
    await wait_steps(sched)


def notes(path: str, sql: str = "SELECT * FROM notifications", args=()):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        return c.execute(sql, args).fetchall()


async def test_mock_order_approval_sends_nothing(db, outbox):
    m = outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1)
    oid = await submit(db, [
        {"id": "s1", "type": "mock.approve",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 3600}}],
        goal="test", repo="/matrix/bench-pilot/repo")
    await run(sched)
    assert m.order_classification(oid) == "test"
    assert notes(db.path, "SELECT 1 FROM approvals WHERE order_id=?", (oid,))
    assert notes(db.path) == []          # владельцу — ничего


async def test_real_approval_with_artifacts_notified(db, outbox):
    m = outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", True,
                                                                  {"patch": "patch.diff"})})
    oid = await submit(db, [
        {"id": "s1", "type": "opencode",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 3600}}])
    await run(sched)
    assert m.order_classification(oid) == "real"
    n = notes(db.path, "SELECT * FROM notifications WHERE kind='approval.requested'")
    assert len(n) == 1
    assert n[0]["order_id"] == oid
    assert n[0]["approval_id"].startswith("apr_")
    assert n[0]["dedup_key"] == f"approval:{n[0]['approval_id']}"


async def test_empty_approval_audited_but_not_notified(db, outbox):
    """«Принять результат? ([])» без артефактов — только аудит, TG молчит."""
    outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", True, {})})
    oid = await submit(db, [
        {"id": "s1", "type": "opencode",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 3600}}])
    await run(sched)
    assert notes(db.path) == []
    ev = [r[0] for r in notes(db.path, "SELECT kind FROM events WHERE order_id=?", (oid,))]
    assert "approval.not_notified" in ev


async def test_approval_ttl_is_not_one_minute(db, outbox):
    outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", True,
                                                                  {"patch": "p"})})
    oid = await submit(db, [
        {"id": "s1", "type": "opencode",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 60}}])
    await run(sched)
    exp, created = notes(db.path, "SELECT expires_at, created FROM approvals"
                                  " WHERE order_id=?", (oid,))[0]
    assert exp - created >= 300


async def test_pause_resume_only_in_audit(db, outbox):
    from src import actions
    outbox.bind(db.path)
    oid = await submit(db, [{"id": "s1", "type": "opencode"}])
    assert await actions.pause_order(db, oid)
    assert await actions.resume_order(db, oid)
    assert notes(db.path) == []
    kinds = [r[0] for r in notes(db.path, "SELECT kind FROM events WHERE order_id=?", (oid,))]
    assert "order.paused" in kinds and "order.resumed" in kinds


async def test_real_done_exactly_one_message(db, outbox):
    outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", True)})
    oid = await submit(db, [{"id": "s1", "type": "opencode"}])
    await run(sched, n=10)
    assert await db.get_order_state(oid) == OrderState.done
    kinds = [r[0] for r in notes(db.path, "SELECT kind FROM notifications")]
    assert kinds == ["order.done"]


async def test_bench_pilot_done_is_silent(db, outbox):
    outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", True)})
    await submit(db, [{"id": "s1", "type": "opencode"}], repo="/matrix/bench-pilot/repo")
    await run(sched, n=10)
    assert notes(db.path) == []


async def test_real_failure_after_exhausted_attempts_notifies(db, outbox):
    outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", False)})
    oid = await submit(db, [
        {"id": "s1", "type": "opencode",
         "retry": {"max_attempts": 1, "backoff_seconds": 0.01}}])
    await run(sched, n=10)
    assert await db.get_order_state(oid) == OrderState.error
    err = notes(db.path, "SELECT * FROM notifications WHERE kind='order.error'")
    assert len(err) == 1
    # в теле — конкретный шаг/класс ошибки, а не сырой dict {'failed': 1}
    assert "s1" in err[0]["body"] and "'failed'" not in err[0]["body"]
    assert notes(db.path, "SELECT 1 FROM notifications WHERE kind='order.done'") == []


async def test_cancelled_order_is_not_critical_error(db, outbox):
    from src import actions
    outbox.bind(db.path)
    sched = Scheduler(db, max_parallel=1,
                      executor_override={"opencode": FakeExecutor("opencode", True)})
    oid = await submit(db, [{"id": "s1", "type": "opencode"}])
    assert await actions.cancel_order(db, oid)
    await run(sched, n=4)
    assert notes(db.path, "SELECT 1 FROM notifications WHERE kind='order.error'") == []
