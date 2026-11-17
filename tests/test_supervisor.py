"""E2: supervisor — снимок, парсинг решений, политика, dry-run."""
import json

import pytest
import yaml

from src.db import DB
from src.models import OrderSpec, StepResult, ErrorClass
from src.states import OrderState, StepState
from src.scheduler import Scheduler
from src import supervisor
from tests.conftest import run_ticks, yaml_order


async def submit(db, steps, goal="t"):
    spec = OrderSpec.model_validate(yaml.safe_load(yaml_order(steps, goal=goal)))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


def test_parse_decision_ok():
    d = supervisor.parse_decision('мусор\n{"action":"retry_step","order_id":"o1","step_id":"s1","reason":"баг флака"}')
    assert d["action"] == "retry_step" and d["order_id"] == "o1"
    assert supervisor.parse_decision('{"action":"delete_everything"}') is None
    assert supervisor.parse_decision("не json") is None
    assert supervisor.parse_decision('{"action":"none"}') == {"action": "none"}


async def test_supervisor_dry_run_and_policy(db, sched, monkeypatch):
    # заказ с failed-шагом (max_attempts=1)
    from src.executors.mock import ScriptedExecutor
    sched.exec["mock"] = ScriptedExecutor({
        "s1": StepResult(ok=False, error_class=ErrorClass.runtime, error_text="x")})
    oid = await submit(db, [{"id": "s1", "type": "mock.fail runtime",
                             "retry": {"max_attempts": 1, "backoff_seconds": 0.1}}])
    await run_ticks(sched, 8, delay=0.15)
    row = (await db.steps_of(oid))[0]
    assert StepState(row["state"]) == StepState.failed

    monkeypatch.setattr(supervisor, "MODE", "dry")
    calls = []

    async def fake_ask(prompt):
        calls.append(prompt)
        return json.dumps({"action": "retry_step", "order_id": oid,
                           "step_id": "s1", "reason": "флак"})
    out = await supervisor.run_once(db, sched, fake_ask)
    assert out["mode"] == "dry" and out["result"] == "dry-run"
    # в dry-run шаг НЕ перезапущен
    row = (await db.steps_of(oid))[0]
    assert StepState(row["state"]) == StepState.failed
    # аудит записан
    cur = await db.conn.execute(
        "SELECT COUNT(*) n FROM events WHERE order_id=? AND kind='supervisor.action'", (oid,))
    assert (await cur.fetchone())["n"] == 1
    # в промпте был снимок
    assert oid in calls[0]


async def test_supervisor_respects_intervention_limit(db, sched, monkeypatch):
    from src.executors.mock import ScriptedExecutor
    sched.exec["mock"] = ScriptedExecutor({
        "s1": StepResult(ok=False, error_class=ErrorClass.runtime, error_text="x")})
    oid = await submit(db, [{"id": "s1", "type": "mock.fail runtime",
                             "retry": {"max_attempts": 1, "backoff_seconds": 0.1}}])
    await run_ticks(sched, 8, delay=0.15)
    for _ in range(3):
        await db.event("supervisor.action", oid, data={"x": 1})
    monkeypatch.setattr(supervisor, "MODE", "dry")

    async def fake_ask(prompt):
        return json.dumps({"action": "retry_step", "order_id": oid,
                           "step_id": "s1", "reason": "x"})
    out = await supervisor.run_once(db, sched, fake_ask)
    assert out.get("skipped") == "policy: intervention limit"


async def test_supervisor_enforce_retries(db, sched, monkeypatch):
    from src.executors.mock import ScriptedExecutor
    sched.exec["mock"] = ScriptedExecutor({
        "s1": StepResult(ok=False, error_class=ErrorClass.runtime, error_text="x")})
    oid = await submit(db, [{"id": "s1", "type": "mock.fail runtime",
                             "retry": {"max_attempts": 1, "backoff_seconds": 0.1}}])
    await run_ticks(sched, 8, delay=0.15)
    monkeypatch.setattr(supervisor, "MODE", "enforce")
    # второй запуск — успех
    class OkOnce(ScriptedExecutor):
        async def run(self, step, workdir=None):
            return StepResult(ok=True)
    sched.exec["mock"] = OkOnce()

    async def fake_ask(prompt):
        return json.dumps({"action": "retry_step", "order_id": oid,
                           "step_id": "s1", "reason": "флак"})
    out = await supervisor.run_once(db, sched, fake_ask)
    assert out["result"] == "True"
    await run_ticks(sched, 8, delay=0.2)
    row = (await db.steps_of(oid))[0]
    assert StepState(row["state"]) == StepState.completed
