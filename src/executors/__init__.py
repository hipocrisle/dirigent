"""Реестр исполнителей: тег -> экземпляр. Добавляется только Kimi (интегратор)."""
from __future__ import annotations

from .base import Executor
from .codex import CodexExecutor
from .mock import MockExecutor, ScriptedExecutor
from .opencode import OpencodeExecutor

registry: dict[str, Executor] = {
    "mock": MockExecutor(),
    "scripted": ScriptedExecutor(),
    "codex": CodexExecutor(),
    "opencode": OpencodeExecutor(),
}


def register(tag: str, ex: Executor) -> None:
    registry[tag] = ex
