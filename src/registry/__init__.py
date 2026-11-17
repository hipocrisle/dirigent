"""Executor registry / capability routing.

Минимальный модуль выбора исполнителя (codex | opencode | gemini) по
фактическому probe(), политике ролей и бюджету запусков. Не создаёт второй
CLI: gemini-адаптер — единственное дополнение, codex/opencode переиспользуются
как есть из src.executors.

Точка входа для интегратора (Kimi): `ExecutorRegistry` + `RegistryScheduler`.
"""
from __future__ import annotations

from .audit import AuditLog, AuditRecord
from .capabilities import KNOWN_ROLES, Capability, Role
from .gemini import GeminiExecutor
from .probe import ProbeResult, QuotaState
from .registry import (
    DEFAULT_PROBE_TTL,
    MAX_LAUNCHES,
    MAX_PARALLEL,
    ExecutorRegistry,
    LaunchAttempt,
    LaunchOutcome,
    Selection,
    SelectionError,
)
from .scheduler import RegistryScheduler

__all__ = [
    "AuditLog",
    "AuditRecord",
    "Capability",
    "DEFAULT_PROBE_TTL",
    "ExecutorRegistry",
    "GeminiExecutor",
    "KNOWN_ROLES",
    "LaunchAttempt",
    "LaunchOutcome",
    "MAX_LAUNCHES",
    "MAX_PARALLEL",
    "ProbeResult",
    "QuotaState",
    "RegistryScheduler",
    "Role",
    "Selection",
    "SelectionError",
]
