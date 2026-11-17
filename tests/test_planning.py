import asyncio
import sys

import pytest

from src.brain import get_brain
from src.db import DB
from src.models import OrderSpec, StepResult
from src.planning import Planner, PlanningRequest, validate_plan
from src.scheduler import Scheduler
from tests.conftest import run_ticks


def request(**kw):
    data = dict(goal='deliver', definition_of_done=['all artifacts checked'], budget=10,
                launch_cost=1, policy={'routes': {'coder': [
                    {'role': 'coder', 'backend': 'primary'},
                    {'role': 'reviewer', 'backend': 'backup'}]}})
    data.update(kw)
    return PlanningRequest.model_validate(data)


def proposal(tmp_path, deps=None):
    artifact = tmp_path / 'result'
    artifact.write_text('checked')
    return OrderSpec(goal='draft', steps=[dict(
        id=sid, type='ignored', prompt='Produce and check artifact', inputs={'role': 'coder'},
        depends_on=ds, outputs={'result': str(artifact)}, acceptance={'checks': ['artifact']})
        for sid, ds in (deps or {'a': [], 'b': [], 'c': [], 'join': ['a', 'b', 'c']}).items()])


class Backend:
    def __init__(self, result=None, offline=False):
        self.result = result or StepResult(ok=True)
        self.offline = offline
        self.calls = []
        self.active = self.peak = 0

    async def available(self):
        return not self.offline

    async def run(self, step, workdir=None):
        self.calls.append((step.id, step.inputs['role']))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.02)
        self.active -= 1
        return self.result


async def test_three_branches_join_and_parallel_limit(db, tmp_path):
    plan = validate_plan(proposal(tmp_path), request())
    oid = await Planner().approve(db, plan)
    backend = Backend()
    sched = Scheduler(db, executor_override={'primary': backend})
    await sched.tick()
    await asyncio.sleep(.005)
    assert backend.active == 2
    assert (await db.get_step_spec(oid, 'join')).depends_on == ['a', 'b', 'c']
    await run_ticks(sched, 10, .03)
    assert backend.peak == 2
    assert [x[0] for x in backend.calls][-1] == 'join'
    assert (await db.get_order_state(oid)).value == 'done'
    await sched.shutdown()


@pytest.mark.parametrize('failure', ['quota', 'resource', 'network', 'offline', 'missing'])
async def test_failover_is_durable_and_changes_role(db, tmp_path, failure):
    plan = validate_plan(proposal(tmp_path, {'a': []}), request())
    oid = await Planner().approve(db, plan)
    first = Backend(StepResult(ok=False, error_class=failure if failure not in ('offline', 'missing') else 'resource'), offline=failure == 'offline')
    backup = Backend()
    overrides = {'backup': backup}
    if failure != 'missing':
        overrides['primary'] = first
    sched = Scheduler(db, executor_override=overrides)
    await run_ticks(sched, 7, .03)
    assert backup.calls == [('a', 'reviewer')]
    assert (await db.get_order_state(oid)).value == 'done'
    assert (await db.steps_of(oid))[0]['attempts'] == 2
    await sched.shutdown()


async def test_budget_stops_failover_without_loop(db, tmp_path):
    plan = validate_plan(proposal(tmp_path, {'a': []}), request(budget=1))
    oid = await Planner().approve(db, plan)
    backup = Backend()
    sched = Scheduler(db, executor_override={
        'primary': Backend(StepResult(ok=False, error_class='quota')), 'backup': backup})
    await run_ticks(sched, 10, .03)
    assert not backup.calls
    assert (await db.get_order_state(oid)).value == 'error'
    rows = await (await db.conn.execute('SELECT * FROM planning_debits WHERE order_id=?', (oid,))).fetchall()
    assert len(rows) == 1
    await sched.shutdown()


async def test_exhausted_routes_do_not_poll(db, tmp_path):
    oid = await Planner().approve(db, validate_plan(proposal(tmp_path, {'a': []}), request()))
    backend = Backend(StepResult(ok=False, error_class='quota'))
    sched = Scheduler(db, executor_override={'primary': backend, 'backup': backend})
    await run_ticks(sched, 10, .03)
    assert len(backend.calls) == 2
    assert (await db.get_order_state(oid)).value == 'error'
    await sched.shutdown()


async def test_restart_preserves_assignment_and_budget(tmp_path):
    path = tmp_path / 'restart.db'
    db = DB(path)
    await db.open()
    oid = await Planner().approve(db, validate_plan(proposal(tmp_path, {'a': []}), request(budget=2)))
    first = Scheduler(db, executor_override={'primary': Backend(offline=True)})
    await first.tick()
    await asyncio.gather(*first._tasks)
    assert (await db.get_step_spec(oid, 'a')).type == 'backup'
    await first.shutdown()
    await db.close()
    db = DB(path)
    await db.open()
    backup = Backend()
    second = Scheduler(db, executor_override={'backup': backup})
    await second.recover()
    await run_ticks(second, 6, .03)
    assert backup.calls == [('a', 'reviewer')]
    assert (await db.get_order_state(oid)).value == 'done'
    row = await (await db.conn.execute('SELECT SUM(cost) cost FROM planning_debits')).fetchone()
    assert row['cost'] == 2
    await second.shutdown()
    await db.close()


@pytest.mark.parametrize('bad', ['cycle', 'acceptance', 'role', 'budget', 'steps'])
def test_reject_invalid_plans(tmp_path, bad):
    spec, req = proposal(tmp_path), request()
    if bad == 'cycle':
        spec.steps[0].depends_on = ['join']
    elif bad == 'acceptance':
        spec.steps[0].acceptance = {'checks': ['trust_me']}
    elif bad == 'role':
        spec.steps[0].inputs['role'] = 'boss'
    elif bad == 'budget':
        req.budget = 1
    else:
        req.policy.max_steps = 2
    with pytest.raises(ValueError):
        validate_plan(spec, req)


def test_cli_brain_and_replan_bound(tmp_path, monkeypatch):
    spec = proposal(tmp_path, {'a': []})
    script = tmp_path / 'planner.py'
    script.write_text('import sys\nsys.stdin.read()\nprint(' + repr(spec.model_dump_json()) + ')\n')
    monkeypatch.setenv('DIRIGENT_BRAIN', 'cli')
    monkeypatch.setenv('DIRIGENT_PLANNER_CLI', f'{sys.executable} {script}')
    result = Planner(get_brain()).propose(request())
    assert result.steps[0].type == 'primary'

    class Invalid:
        calls = 0
        def plan(self, *args):
            self.calls += 1
            raise ValueError('bad DAG')
    brain = Invalid()
    with pytest.raises(ValueError, match='exhausted'):
        Planner(brain).propose(request())
    assert brain.calls == 3


async def test_lost_running_lease_keeps_debit(db, tmp_path):
    oid = await Planner().approve(db, validate_plan(proposal(tmp_path, {'a': []}), request(budget=1)))
    await db.promote_pending()
    assert await db.lease_step(oid, 'a', 'dead-owner', -1)
    crashed = Scheduler(db)
    assert await crashed.assignment.reserve(oid, 'a')
    restarted = Scheduler(db, executor_override={'primary': Backend()})
    await restarted.recover()
    await db.conn.execute('UPDATE steps SET backoff_until=0 WHERE order_id=?', (oid,))
    await db.conn.commit()
    await run_ticks(restarted, 5, .03)
    assert (await db.get_order_state(oid)).value == 'error'
    assert not restarted.exec['primary'].calls
    await restarted.shutdown()


async def test_call_cap_independent_of_money(db, tmp_path):
    req = request()
    req.policy.max_calls = 1
    oid = await Planner().approve(db, validate_plan(proposal(tmp_path, {'a': []}), req))
    backend = Backend(StepResult(ok=False, error_class='quota'))
    sched = Scheduler(db, executor_override={'primary': backend, 'backup': backend})
    await run_ticks(sched, 7, .03)
    assert len(backend.calls) == 1
    assert (await db.get_order_state(oid)).value == 'error'
    await sched.shutdown()


@pytest.mark.parametrize('startup', [True, False])
async def test_lost_lease_advances_route_without_repeating_backend(db, tmp_path, startup):
    oid = await Planner().approve(db, validate_plan(proposal(tmp_path, {'a': []}), request(budget=2)))
    await db.promote_pending()
    assert await db.lease_step(oid, 'a', 'dead-owner', -1)
    primary, backup = Backend(), Backend()
    sched = Scheduler(db, executor_override={'primary': primary, 'backup': backup})
    assert await sched.assignment.reserve(oid, 'a')
    if startup:
        await sched.recover()
    else:
        await sched._reap_stale_leases()
    await run_ticks(sched, 6, .03)
    assert not primary.calls
    assert backup.calls == [('a', 'reviewer')]
    assert (await db.get_order_state(oid)).value == 'done'
    await sched.shutdown()


async def test_failed_machine_acceptance_cancels_dependents(db, tmp_path):
    spec = proposal(tmp_path, {'a': [], 'join': ['a']})
    spec.steps[0].outputs = {'result': str(tmp_path / 'missing')}
    oid = await Planner().approve(db, validate_plan(spec, request()))
    backend = Backend()
    sched = Scheduler(db, executor_override={'primary': backend})
    await run_ticks(sched, 6, .03)
    assert backend.calls == [('a', 'coder')]
    rows = {row['id']: row['state'] for row in await db.steps_of(oid)}
    assert rows == {'a': 'failed', 'join': 'cancelled'}
    await sched.shutdown()


@pytest.mark.parametrize('files', ['result.txt', [1], [''], []])
def test_reject_malformed_checker_parameters(tmp_path, files):
    spec = proposal(tmp_path, {'a': []})
    spec.steps[0].acceptance = {'checks': ['files_exist'], 'expect_files': files}
    with pytest.raises(ValueError):
        validate_plan(spec, request())


@pytest.mark.parametrize('routes', [[], [
    {'role': 'reviewer', 'backend': 'backup'}], [
    {'role': 'coder', 'backend': 'primary'},
    {'role': 'coder', 'backend': 'primary'}]])
def test_reject_invalid_policy_routes(routes):
    with pytest.raises(ValueError):
        request(policy={'routes': {'coder': routes}})
