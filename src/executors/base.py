"""Интерфейс исполнителей. ЗАМОРОЖЕН до подключения WP-2 (меняет только Kimi).

Новый исполнитель = модуль с классом, реализующим Executor, плюс регистрация
тега в executors/__init__.py::registry. Адаптер НЕ угадывает: любая непонятная
ошибка -> ErrorClass.unknown, а не «похоже на сеть».
"""
from __future__ import annotations

from typing import Protocol

from ..models import ErrorClass, StepResult, StepSpec


class Executor(Protocol):
    tag: str  # "codex", "opencode", "shell", ...

    async def available(self) -> bool:
        """False -> шаг уходит в waiting_resource (не failed!)."""
        ...

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        """Запуск до завершения (свой внутренний таймаут <= step.timeout_seconds).
        Обязан вернуть StepResult; исключения ловит сам и возвращает unknown/runtime.

        Правило результата: rc==0 с пустым stdout — НЕ успех, а runtime
        («молчаливый успех» недопустим, фиксируется в интерфейсе, чтобы
        реализации не расходились)."""
        ...

    async def status(self, run_id: str) -> StepResult | None:
        """Для восстановления после падения демона: что стало с внешним запуском.
        None = неизвестно/не поддерживается."""
        ...

    async def cancel(self, run_id: str) -> None:
        ...


def classify_returncode(rc: int) -> ErrorClass:
    """Грубая разметка по коду выхода; уточняется адаптером по stderr."""
    if rc == 0:
        return ErrorClass.ok
    if rc in (124, 137, 143):
        return ErrorClass.timeout
    return ErrorClass.runtime
