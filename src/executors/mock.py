"""Mock-исполнители для вертикального среза (фаза 1 спеки).

Поведение кодируется в step.type:
  mock.ok [delay_s]           — успех через N сек (дефолт 0.05)
  mock.fail [class] [delay]   — ошибка заданного класса
  mock.quota [reset_in_s]     — квота, reset_at = now+reset_in_s (None если не указано)
  mock.approve                — успех, но требует approval after
Плюс ScriptedExecutor для тестов: сценарий по id шага.
"""
from __future__ import annotations

import asyncio
import time

from ..models import ErrorClass, StepResult, StepSpec
from .base import Executor


class MockExecutor:
    tag = "mock"

    async def available(self) -> bool:
        return True

    async def status(self, run_id: str) -> StepResult | None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        parts = step.type.split(".", 1)[1].split() if "." in step.type else ["ok"]
        kind = parts[0]
        arg = parts[1] if len(parts) > 1 else None
        delay = 0.05
        if kind in ("ok", "fail", "quota") and arg is not None:
            try:
                delay = float(arg)
                arg = None
            except ValueError:
                pass
        await asyncio.sleep(delay)

        if kind == "ok":
            return StepResult(ok=True)
        if kind == "fail":
            cls = ErrorClass(arg) if arg else ErrorClass.runtime
            return StepResult(ok=False, error_class=cls, error_text=f"mock {cls.value}")
        if kind == "quota":
            reset = time.time() + float(arg) if arg else None
            return StepResult(
                ok=False, error_class=ErrorClass.quota,
                error_text="mock: quota exhausted",
                quota_reset_at=reset,
            )
        if kind == "approve":
            return StepResult(ok=True)
        return StepResult(ok=False, error_class=ErrorClass.unknown,
                          error_text=f"mock: unknown kind {kind}")


class ScriptedExecutor:
    """Для тестов: результат по id шага из словаря; шага нет -> ok."""

    tag = "scripted"

    def __init__(self, script: dict[str, StepResult] | None = None):
        self.script = script or {}

    async def available(self) -> bool:
        return True

    async def status(self, run_id: str) -> StepResult | None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        await asyncio.sleep(0.01)
        res = self.script.get(step.id)
        if res is None:
            return StepResult(ok=True)
        return res
