"""Реестр исполнителей: тег -> экземпляр. Добавляется только Kimi (интегратор)."""
from __future__ import annotations

from .base import Executor
from .mock import MockExecutor, ScriptedExecutor

registry: dict[str, Executor] = {
    "mock": MockExecutor(),
    "scripted": ScriptedExecutor(),
}


def register(tag: str, ex: Executor) -> None:
    registry[tag] = ex
