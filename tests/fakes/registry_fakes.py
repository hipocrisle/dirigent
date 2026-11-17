"""Fake-адаптеры исполнителей для тестов registry (без реальных CLI).

FakeExecutor повторяет контракт src.executors.base.Executor:
  available(), run(step, workdir), status(), cancel().
Поведение задаётся сценарием по StepSpec.id и/или очередью результатов.
Каждый run() синхронно учитывает пик параллелизма на общем счётчике, чтобы
тест на ограничение потолка читал факт, а не веру.
"""
from __future__ import annotations

import asyncio
import time

from src.models import ErrorClass, StepResult, StepSpec


class FakeExecutor:
    def __init__(self, tag: str, *, installed: bool = True,
                 authenticated: bool = True, script=None,
                 delay: float = 0.0, counter: dict | None = None,
                 binary: str | None = "fake-binary"):
        self.tag = tag
        self.binary = binary  # для probe: shutil.which; None => судим по available
        self._installed = installed
        self._authenticated = authenticated
        self.script = script or {}      # step.id -> StepResult | callable
        self.delay = delay
        self.calls: list[tuple[str, StepSpec, str | None]] = []
        self.started: list[float] = []
        self.finished: list[float] = []
        self._counter = counter if counter is not None else {"now": 0, "peak": 0}
        self._lock = asyncio.Lock()

    async def is_installed(self) -> bool:  # совместимо с probe
        return self._installed

    async def available(self) -> bool:
        return self._installed and self._authenticated

    async def status(self, run_id: str):
        return None

    async def cancel(self, run_id: str) -> None:
        return None

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        self.calls.append((self.tag, step, workdir))
        self.started.append(time.monotonic())
        async with self._lock:
            self._counter["now"] += 1
            self._counter["peak"] = max(self._counter["peak"], self._counter["now"])
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            res = self.script.get(step.id)
            if callable(res):
                res = res(step)
            if res is None:
                res = StepResult(ok=True)
        finally:
            async with self._lock:
                self._counter["now"] -= 1
            self.finished.append(time.monotonic())
        return res


def fail(error_class: ErrorClass, text: str = "", **kw) -> StepResult:
    return StepResult(ok=False, error_class=error_class, error_text=text, **kw)


def ok(**kw) -> StepResult:
    return StepResult(ok=True, **kw)


def step(sid: str = "s1", *, type: str = "registry", prompt: str = "p",
         role: str | None = None, timeout: float = 10.0) -> StepSpec:
    inputs = {}
    if role:
        inputs["role"] = role
    return StepSpec(id=sid, type=type, prompt=prompt, inputs=inputs,
                    timeout_seconds=timeout)
