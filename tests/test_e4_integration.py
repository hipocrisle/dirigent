"""Offline E4: existing Planner -> SQLite -> RegistryScheduler -> mock adapters."""
import asyncio
import json
import socket
import subprocess

import pytest

from src.planning import Planner
from src.registry import ExecutorRegistry, FakeProber, make_report
from src.registry.scheduler import RegistryScheduler
from tests.conftest import run_ticks
from tests.test_planning import Backend, proposal, request


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('external I/O forbidden in E4 tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


class Brain:
    def __init__(self, spec):
        self.spec = spec
        self.requests = []

    def plan(self, text, repo=None):
        self.requests.append(json.loads(text))
        return self.spec


def scheduler(db, **executors):
    registry = ExecutorRegistry(probers={
        tag: FakeProber(make_report(tag)) for tag in executors})
    return RegistryScheduler(db, max_parallel=2, executor_override=executors,
                             planning_registry=registry)


async def test_goal_dag_registry_parallel_and_dependencies(db, tmp_path):
    # Distinct executor objects synchronize at a barrier: join cannot start early.
    entered, release = asyncio.Event(), asyncio.Event()
    running = set()
    finished = set()

    class Worker(Backend):
        async def run(self, step, workdir=None):
            if step.id == 'join':
                assert finished == {'a', 'b'}
            else:
                running.add(step.id)
                if len(running) == 2:
                    entered.set()
                await release.wait()
                finished.add(step.id)
            return await super().run(step, workdir)

    first, second = Worker(), Worker()
    # Both distinct mock workers are exercised via approved role routes.
    spec = proposal(tmp_path, {'a': [], 'b': [], 'join': ['a', 'b']})
    spec.steps[1].inputs['role'] = 'tester'
    req = request(policy={'routes': {
        'coder': [{'role': 'coder', 'backend': 'primary'}],
        'tester': [{'role': 'tester', 'backend': 'secondary'}]}})
    brain = Brain(spec)
    req.goal = "Подготовь два независимых артефакта и проверь общий результат"
    oid = await Planner(brain).approve(db, Planner(brain).propose(req))
    assert brain.requests == [req.model_dump()]
    sched = scheduler(db, primary=first, secondary=second)
    try:
        await sched.tick()
        await asyncio.wait_for(entered.wait(), 1)
        assert running == {'a', 'b'}
        assert not finished
        assert len(sched._tasks) == 2
        assert (await db.get_step_spec(oid, 'join')).depends_on == ['a', 'b']
        release.set()
        await run_ticks(sched, 8, .03)
        assert (await db.get_order_state(oid)).value == 'done'
        assert first.calls == [('a', 'coder'), ('join', 'coder')]
        assert second.calls == [('b', 'tester')]
        events = await (await db.conn.execute(
            "SELECT data_json FROM events WHERE order_id=? AND kind='planning.selection'", (oid,))).fetchall()
        assert len(events) == 3
    finally:
        release.set()
        await sched.shutdown()


@pytest.mark.parametrize('cap', [1, 2])
async def test_registry_budget_and_restart(tmp_path, cap):
    from src.db import DB
    from src.models import StepResult
    path = tmp_path / 'e4.db'
    db = DB(path)
    await db.open()
    brain = Brain(proposal(tmp_path, {'a': []}))
    req = request(budget=cap)
    oid = await Planner(brain).approve(db, Planner(brain).propose(req))
    first = scheduler(db, primary=Backend(StepResult(ok=False, error_class='quota')))
    await first.tick()
    await asyncio.gather(*first._tasks)
    await first.shutdown()
    await db.close()
    db = DB(path)
    await db.open()
    backup = Backend()
    second = scheduler(db, backup=backup)
    try:
        await second.recover()
        await run_ticks(second, 6, .03)
        assert backup.calls == ([] if cap == 1 else [('a', 'reviewer')])
        assert (await db.get_order_state(oid)).value == ('error' if cap == 1 else 'done')
        row = await (await db.conn.execute('SELECT SUM(cost) FROM planning_debits')).fetchone()
        assert row[0] == cap
    finally:
        await second.shutdown()
        await db.close()
