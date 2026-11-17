"""Интеграция registry с планировщиком: failover после quota/network.

Не меняет scheduler.py: registry используется как «мозг выбора» поверх уже
существующих адаптеров. Проверяем, что последовательность note_quota/note_network
+ can_launch даёт ровно 3 запуска на шаг и переключение на другого исполнителя.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from src.db import DB
from src.executors.mock import ScriptedExecutor
from src.models import ErrorClass, OrderSpec, StepResult
from src.registry import ExecutorRegistry, FakeProber, make_report
from src.registry.bridge import failover_decision
from src.scheduler import Scheduler
from src.states import OrderState


class FailFirst(ScriptedExecutor):
    """Первый вызов -> quota; второй -> ok. Считает запуски."""

    def __init__(self, cls=ErrorClass.quota):
        self.cls = cls
        self.calls = 0

    async def available(self):
        return True

    async def run(self, step, workdir=None):
        self.calls += 1
        if self.calls == 1:
            return StepResult(ok=False, error_class=self.cls, error_text="fail once")
        return StepResult(ok=True)


async def _submit(db: DB, steps: list[dict]) -> str:
    spec = OrderSpec.model_validate(yaml.safe_load(
        yaml.safe_dump({"goal": "t", "steps": steps}, allow_unicode=True)))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


@pytest.mark.asyncio
async def test_failover_quota_within_three_launches(db):
    """Шаг: quota -> registry фиксирует, can_launch держит <=3, затем ok."""
    reg = ExecutorRegistry(probers={
        "codex": FakeProber(make_report("codex")),
        "opencode": FakeProber(make_report("opencode")),
    })
    await reg.probe()
    key = "o/s1"
    ex = FailFirst(ErrorClass.quota)
    res = await ex.run(None)  # 1-й запуск -> quota
    assert failover_decision(reg, key, "codex", res) is True
    reg.note_launch(key, "codex")
    assert reg.launches_for(key) == 1
    # переключение на opencode (codex в карантине)
    sel = await reg.select("coding", key=key)
    assert sel.tag == "opencode"


@pytest.mark.asyncio
async def test_three_launches_are_hard_cap(db):
    reg = ExecutorRegistry(probers={
        "codex": FakeProber(make_report("codex")),
        "opencode": FakeProber(make_report("opencode")),
    })
    await reg.probe()
    key = "o/s2"
    launches = 0
    for _ in range(5):
        if not reg.can_launch(key):
            break
        launches += 1
        reg.note_launch(key, "opencode")
        reg.note_network("opencode", cooldown_seconds=0)
    assert launches == 3


@pytest.mark.asyncio
async def test_scheduler_still_runs_without_registry(db, sched):
    """Регресс: планировщик без registry работает как раньше (мок-шаг)."""
    oid = await _submit(db, [{"id": "s1", "type": "mock.ok"}])
    for _ in range(6):
        await sched.tick()
        await asyncio.sleep(0.05)
    assert await db.get_order_state(oid) == OrderState.done
