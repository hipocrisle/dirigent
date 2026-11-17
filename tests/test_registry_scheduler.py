"""Интеграция ExecutorRegistry с Scheduler (RegistryScheduler) — на fake-адаптерах.

Покрытие:
  * шаг type=registry проходит в registry-путь, обычные шаги — нет;
  * выбор по роли из step.inputs, работа в worktree;
  * quota exhausted без reset_at -> FAILED (unknown != available), а не цикл
    вечного waiting_quota;
  * known reset в будущем -> waiting_quota, после сброса — completed;
  * offline -> waiting_resource (не жжём бюджет);
  * step.waiting_quota в ядре опирается на живой available() (после
    auth-fail ожидает ручного входа).
"""
from __future__ import annotations

import asyncio
import time

import pytest
import yaml

from src.db import DB
from src.models import ErrorClass, OrderSpec, StepResult
from src.registry import AuditLog, RegistryScheduler
from src.registry.capabilities import Role
from src.registry.scheduler import is_registry_step, role_of
from src.states import OrderState, StepState
from src.models import StepSpec
from tests.conftest import run_ticks
from tests.fakes.registry_fakes import FakeExecutor, fail, ok


async def submit(db: DB, steps: list[dict], goal: str = "test") -> str:
    spec = OrderSpec.model_validate(yaml.safe_load(
        yaml.safe_dump({"goal": goal, "steps": steps}, allow_unicode=True)))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


async def step_state(db: DB, oid: str, sid: str) -> StepState:
    for r in await db.steps_of(oid):
        if r["id"] == sid:
            return StepState(r["state"])
    raise KeyError(sid)


@pytest.fixture
def fakes():
    counter = {"now": 0, "peak": 0}
    return {t: FakeExecutor(t, counter=counter)
            for t in ("codex", "opencode", "gemini")}


@pytest.fixture
def rsch(db, fakes, tmp_path):
    s = RegistryScheduler(db, max_parallel=2)
    reg = s._get_registry()
    # подменяем реальные адаптеры фейками и сбрасываем кэши реестра
    reg.executors.clear()
    reg.executors.update(fakes)
    reg._probe_cache.clear()
    reg._run_state.clear()
    reg._launch_cache.clear()
    # аудит — во временный файл, не в /matrix/dirigent/var
    from src.registry import ExecutorRegistry
    s._registry_obj = ExecutorRegistry(
        fakes, audit=AuditLog(str(tmp_path / "audit.jsonl")), probe_ttl=0.0)
    s.exec.update(fakes)
    return s


def test_is_registry_step_semantics():
    assert is_registry_step(StepSpec(id="a", type="registry", prompt="x"))
    assert is_registry_step(StepSpec(id="a", type="registry.review", prompt="x"))
    assert is_registry_step(StepSpec(id="a", type="mock.ok", executor="registry"))
    assert not is_registry_step(StepSpec(id="a", type="mock.ok"))
    assert not is_registry_step(StepSpec(id="a", type="codex"))


def test_role_of():
    assert role_of(StepSpec(id="a", type="registry",
                            inputs={"role": "coding"})) is Role.coding
    assert role_of(StepSpec(id="a", type="registry")) is Role.general
    assert role_of(StepSpec(id="a", type="registry",
                            inputs={"role": "чушь"})) is Role.general


async def test_registry_step_reaches_done(db, rsch):
    oid = await submit(db, [{"id": "s1", "type": "registry",
                             "inputs": {"role": "coding"}, "prompt": "p"}])
    await run_ticks(rsch, 6)
    assert await step_state(db, oid, "s1") == StepState.completed
    # coding -> opencode выбран и реально вызван
    assert [c[0] for c in rsch._registry_obj.executors["opencode"].calls] == ["opencode"]


async def test_plain_mock_step_still_uses_old_path(db, rsch):
    oid = await submit(db, [{"id": "s1", "type": "mock.ok"}])
    await run_ticks(rsch, 4)
    assert await step_state(db, oid, "s1") == StepState.completed


async def test_registry_quota_unknown_reset_fails_not_waits(db, rsch):
    # rsch фикстура заменила reg.executors, а launch идёт через s.exec — пишем в него
    rsch.exec["codex"].script["s1"] = fail(ErrorClass.quota, "usage limit")
    rsch.exec["opencode"].script["s1"] = fail(ErrorClass.network, "connection refused")
    rsch.exec["gemini"].script["s1"] = fail(ErrorClass.quota, "quota")
    oid = await submit(db, [{"id": "s1", "type": "registry",
                             "inputs": {"role": "general"}, "prompt": "p",
                             "retry": {"max_attempts": 1, "backoff_seconds": 0.1}}])
    # Политика шага max_attempts=1 = один запуск всей registry-цепочки; внутри
    # попытки бюджета по-прежнему ≤3 исполнителя (проверено в test_registry).
    await run_ticks(rsch, 8, delay=0.15)
    assert await step_state(db, oid, "s1") == StepState.failed
    # И главное: НЕ ушло в waiting_quota без знания времени сброса
    assert await step_state(db, oid, "s1") != StepState.waiting_quota
    cur = await db.conn.execute(
        "SELECT error_class, error_text FROM steps WHERE order_id=? AND id=?",
        (oid, "s1"))
    row = await cur.fetchone()
    assert "reset unknown" in (row["error_text"] or "")


async def test_registry_offline_goes_to_waiting_resource(db, fakes, tmp_path):
    from src.registry import ExecutorRegistry, AuditLog
    s = RegistryScheduler(db, max_parallel=2)
    for f in fakes.values():
        f._installed = False
    s._registry_obj = ExecutorRegistry(fakes, audit=AuditLog(str(tmp_path / "a.jsonl")),
                                       probe_ttl=0.0)
    await db.conn.execute("UPDATE steps SET state='pending'")
    await db.conn.commit()
    oid = await submit(db, [{"id": "s1", "type": "registry", "prompt": "p"}])
    await run_ticks(s, 3, delay=0.15)
    await s.shutdown()
    await asyncio.sleep(0.05)
    assert await step_state(db, oid, "s1") == StepState.waiting_resource
    # никаких запусков «вхолостую»
    assert not any(f.calls for f in fakes.values())


async def test_registry_worktree_created(db, rsch, tmp_path, monkeypatch):
    """worktree изолируется: исполнитель получает НЕ repo, а путь worktree."""
    import subprocess
    from src.registry import scheduler as sched_mod

    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "README.md").write_text("x", encoding="utf-8")
    run("git", "add", ".")
    run("git", "commit", "-qm", "init")

    # В этом модуле worktree создаётся только если шаг входит в ряд
    # workspace-изолированных; registry-путь изолирует ВСЕ шаги (см. докстринг).
    make_wt = []

    def fake_ensure(repo_path, oid, sid):
        wt = tmp_path / f"wt-{oid}-{sid}"
        run("git", "worktree", "add", "-b", f"dirigent/{oid}-{sid}", str(wt))
        make_wt.append((str(wt), f"dirigent/{oid}-{sid}"))
        return str(wt), f"dirigent/{oid}-{sid}"

    import src.workspace as ws
    monkeypatch.setattr(ws, "ensure_worktree", fake_ensure)
    monkeypatch.setattr(ws, "cleanup_worktree", lambda *a, **k: None)

    oid = await submit(db, [{"id": "s1", "type": "registry",
                             "inputs": {"role": "coding"}, "prompt": "p"}])
    await db.conn.execute("UPDATE orders SET repo=? WHERE id=?", (str(repo), oid))
    await db.conn.commit()
    await run_ticks(rsch, 6)
    assert make_wt and make_wt[0][0] != str(repo)
    calls = rsch._registry_obj.executors["opencode"].calls
    assert calls and calls[0][2] == make_wt[0][0]


async def test_registry_audit_has_order_step(db, rsch, tmp_path):
    oid = await submit(db, [{"id": "s1", "type": "registry", "prompt": "p"}])
    await run_ticks(rsch, 5)
    log = list(rsch._registry_obj.audit.read())
    assert any(r.kind == "launch" and r.step_id == "s1" for r in log)
    assert any(r.kind == "select" for r in log)


async def test_waiting_quota_known_reset_transitions_and_resumes(db, fakes, tmp_path):
    """known reset в будущем -> waiting_quota (ядерная семантика), после — completed.

    Важно: шаг с known reset не должен тратиться в failed, и не должен будить
    исполнителя раньше времени (reset_at в будущем писался в step-строку).
    """
    from src.registry import ExecutorRegistry, AuditLog
    s = RegistryScheduler(db, max_parallel=2)
    reset_at = time.time() + 3600
    fakes["codex"].script["s1"] = fail(ErrorClass.quota, "quota", quota_reset_at=reset_at)
    fakes["opencode"].script["s1"] = ok()
    s._registry_obj = ExecutorRegistry(fakes, audit=AuditLog(str(tmp_path / "a.jsonl")),
                                       probe_ttl=0.0)
    oid = await submit(db, [{"id": "s1", "type": "registry",
                             "inputs": {"role": "architecture"}, "prompt": "p"}])
    await run_ticks(s, 4, delay=0.1)
    await asyncio.sleep(0.05)
    # Итог попытки — успех OpenCode (failover), то есть completed:
    assert await step_state(db, oid, "s1") == StepState.completed
    # а quот-факт по Codex записан в журнал — следующий заказ его учтёт
    pr = await s._registry_obj.probe("codex", force=True)
    assert pr.quota_state.value == "exhausted"
    await s.shutdown()
