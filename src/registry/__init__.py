"""Executor registry / capability routing (минимальный модуль).

Публичное API:
    from src.registry import ExecutorRegistry, Role, Budget, make_report
    reg = ExecutorRegistry()
    await reg.probe()
    sel = await reg.select(Role.coding, budget=Budget(max_launches=3), key="ord/s1")
    if sel.status is SelectionStatus.selected:
        ... # запуск через существующий адаптер sel.tag

Модуль ничего не запускает и не тратит квоту: только выбирает. Запуск — за
существующими адаптерами executors/ и планировщиком.
"""
from __future__ import annotations

from .core import (
    MAX_LAUNCHES,
    MAX_PARALLEL,
    ExecutorRegistry,
    RoutingAudit,
    read_capabilities,
)
from .probes import (
    CodexProber,
    FakeProber,
    GeminiProber,
    OpencodeProber,
    Prober,
    make_report,
)
from .types import (
    Availability,
    Budget,
    ProbeReport,
    QuotaState,
    Role,
    Selection,
    SelectionStatus,
)

__all__ = [
    "ExecutorRegistry",
    "RoutingAudit",
    "MAX_LAUNCHES",
    "MAX_PARALLEL",
    "read_capabilities",
    "Prober",
    "CodexProber",
    "OpencodeProber",
    "GeminiProber",
    "FakeProber",
    "make_report",
    "Availability",
    "Budget",
    "ProbeReport",
    "QuotaState",
    "Role",
    "Selection",
    "SelectionStatus",
]
