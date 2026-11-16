"""P1: worktree-изоляция и мок-сценарий квоты Codex (тест B спеки)."""
import asyncio
import subprocess

import pytest
import yaml

from src.db import DB
from src.executors.mock import ScriptedExecutor
from src.models import ErrorClass, OrderSpec, StepResult
from src.scheduler import Scheduler
from src.states import OrderState, StepState
from tests.conftest import run_ticks, yaml_order


@pytest.fixture
def git_repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "go.mod").write_text("module t\n\ngo 1.22\n")
    (d / "a.go").write_text("package t\n\nfunc A() int { return 1 }\n")
    subprocess.run(["git", "init", "-q", str(d)])
    subprocess.run(["git", "-C", str(d), "add", "-A"])
    subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-qm", "base"])
    return str(d)


async def submit(db, steps, goal="t", repo=None):
    spec = OrderSpec.model_validate(
        yaml.safe_load(yaml_order(steps, goal=goal, **({"repo": repo} if repo else {}))))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


class Writer(ScriptedExecutor):
    """Имитация coding-исполнителя: пишет файл в workdir."""

    async def run(self, step, workdir=None):
        import pathlib
        pathlib.Path(workdir, "new.go").write_text("package t\n")
        return StepResult(ok=True)


async def test_worktree_isolate_and_merge(db, sched, git_repo):
    sched.exec["opencode"] = Writer()
    oid = await submit(db, [{"id": "s1", "type": "opencode",
                             "acceptance": {"checks": ["repo_changed"]}}], repo=git_repo)
    await run_ticks(sched, 8, delay=0.15)

    assert await db.get_order_state(oid) == OrderState.done
    # основная ветка НЕ тронута
    main = subprocess.run(["git", "-C", git_repo, "log", "--oneline", "-1"],
                          capture_output=True, text=True).stdout.strip()
    assert main.endswith("base")
    # интеграционная ветка содержит изменение
    r = subprocess.run(["git", "-C", git_repo, "show", "dirigent/integrate:new.go"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    # worktree подчищен
    import pathlib
    from src.workspace import BASE
    assert not list(BASE.glob(f"{oid}-*"))


async def test_quota_failover(db, sched):
    """Тест B: Codex в квоте -> независимый шаг уходит на другого исполнителя ->
    после reset Codex возобновляется БЕЗ повторного выполнения готового."""

    class CodexQuota(ScriptedExecutor):
        def __init__(self):
            self.calls = 0

        async def available(self):
            return True

        async def run(self, step, workdir=None):
            import time
            self.calls += 1
            if self.calls == 1:
                return StepResult(ok=False, error_class=ErrorClass.quota,
                                  error_text="usage limit reached",
                                  quota_reset_at=time.time() + 1.2)
            await asyncio.sleep(0.05)
            return StepResult(ok=True)

    class DeepseekSlow(ScriptedExecutor):
        def __init__(self):
            self.calls = 0

        async def run(self, step, workdir=None):
            self.calls += 1
            await asyncio.sleep(0.5)
            return StepResult(ok=True)

    codex, deep = CodexQuota(), DeepseekSlow()
    sched.exec["codex"] = codex
    sched.exec["opencode"] = deep

    oid = await submit(db, [
        {"id": "c1", "type": "codex"},
        {"id": "d1", "type": "opencode"},
    ])
    # гоним тики; фиксируем момент, когда d1 завершён, а c1 ещё ждёт квоту
    saw_quota = False
    for _ in range(30):
        await sched.tick()
        await asyncio.sleep(0.25)
        st = {r["id"]: StepState(r["state"]) for r in await db.steps_of(oid)}
        if st["d1"] == StepState.completed and st["c1"] == StepState.waiting_quota:
            saw_quota = True
        if st["c1"] == StepState.completed and st["d1"] == StepState.completed:
            break

    assert saw_quota, "независимый шаг не выполнился, пока Codex ждал квоту"
    await sched.tick()  # финальный проход: _update_orders переведёт заказ в done
    st = {r["id"]: StepState(r["state"]) for r in await db.steps_of(oid)}
    assert st["c1"] == StepState.completed and st["d1"] == StepState.completed
    assert await db.get_order_state(oid) == OrderState.done
    # c1: запуск с квотой + запуск после восстановления; d1 — ровно один
    assert codex.calls == 2
    assert deep.calls == 1


class FailForever(ScriptedExecutor):
    async def run(self, step, workdir=None):
        await asyncio.sleep(0.02)
        return StepResult(ok=False, error_class=ErrorClass.runtime, error_text="boom")


async def test_retry_accounting_and_cap(db, sched):
    """Дефект drill: attempts монотонно считает ФАКТИЧЕСКИЕ запуски; потолок 3;
    4-й retry отказ; рестарт демона счётчик не сбрасывает."""
    from src import actions
    from src.policy import MAX_STEP_LAUNCHES
    sched.exec["mock"] = FailForever()
    oid = await submit(db, [{"id": "s1", "type": "mock.fail runtime",
                             "retry": {"max_attempts": 2, "backoff_seconds": 0.1}}])
    await run_ticks(sched, 10, delay=0.15)
    row = (await db.steps_of(oid))[0]
    assert StepState(row["state"]) == StepState.failed
    assert row["attempts"] == 2, f"auto: 2 запуска, got {row['attempts']}"

    assert await actions.retry_step(db, oid, "s1")          # 3-й запуск
    await run_ticks(sched, 8, delay=0.2)
    row = (await db.steps_of(oid))[0]
    assert row["attempts"] == 3 and StepState(row["state"]) == StepState.failed

    assert not await actions.retry_step(db, oid, "s1")      # 4-й — отказ
    row = (await db.steps_of(oid))[0]
    assert row["attempts"] == 3

    # рестарт демона: счётчик и состояние сохраняются
    sched2 = Scheduler(db)
    await sched2.recover()
    row = (await db.steps_of(oid))[0]
    assert row["attempts"] == 3 and StepState(row["state"]) == StepState.failed

    # аудит: число lease-событий == числу запусков
    cur = await db.conn.execute(
        "SELECT COUNT(*) n FROM events WHERE order_id=? AND step_id='s1' AND kind='step.lease'",
        (oid,))
    assert (await cur.fetchone())["n"] == MAX_STEP_LAUNCHES
    cur2 = await db.conn.execute(
        "SELECT COUNT(*) n FROM events WHERE order_id=? AND kind='step.retry_refused'",
        (oid,))
    assert (await cur2.fetchone())["n"] == 1
