"""Аудит маршрутизации: кто выбран/запущен/провалился и ПОЧЕМУ.

Журнал — JSONL, дописывается только в конец. Каждая запись:
  ts, kind (probe|select|launch|launch_result|exhausted),
  executor, role, reason, data, order_id, step_id, attempt.

Это не оценка качества и не метрики квоты: только трассируемая цепочка
решений, по которой человек/Kimi может ответить «почему сработал Codex,
а не Gemini» на конкретном заказе.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

DEFAULT_AUDIT_PATH = os.environ.get(
    "DIRIGENT_REGISTRY_AUDIT", "/matrix/dirigent/var/registry-audit.jsonl")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(os.path.abspath(path), threading.Lock())


@dataclass(frozen=True)
class AuditRecord:
    at: float
    kind: str
    executor: str | None = None
    role: str | None = None
    reason: str = ""
    order_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


class AuditLog:
    """Короткий append-only JSONL. Ошибки записи не валят планировщик
    (аудит не должен становиться точкой отказа), но логируются вызывающим."""

    def __init__(self, path: str = DEFAULT_AUDIT_PATH):
        self.path = path

    def append(self, kind: str, *, executor: str | None = None,
               role: str | None = None, reason: str = "",
               order_id: str | None = None, step_id: str | None = None,
               attempt: int | None = None, at: float | None = None,
               data: dict[str, Any] | None = None,
               **extra: Any) -> AuditRecord:
        """`data` — структурированное содержимое записи; `extra` — сахар.

        Оба параметра поддерживаются, чтобы вызывающий не думал о том, куда
        положить ключи — они всё равно попадают в `data` записи.
        """
        d = dict(data or {})
        d.update(extra)
        rec = AuditRecord(at=at if at is not None else time.time(), kind=kind,
                          executor=executor, role=role, reason=reason,
                          order_id=order_id, step_id=step_id, attempt=attempt,
                          data=d)
        uzkij = os.path.dirname(self.path)
        if uzkij:
            os.makedirs(uzkij, exist_ok=True)
        with _lock_for(self.path):
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(rec.to_json() + "\n")
        return rec

    def read(self, order_id: str | None = None) -> Iterator[AuditRecord]:
        if not os.path.exists(self.path):
            return
        with _lock_for(self.path):
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if order_id is not None and d.get("order_id") != order_id:
                continue
            yield AuditRecord(**d)

    def read_tail(self, limit: int = 500) -> list[AuditRecord]:
        """Последние `limit` записей (быстрый доступ для probe после рестарта)."""
        out: list[AuditRecord] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                buf = f.readlines()[-limit:]
        except FileNotFoundError:
            return []
        for line in buf:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(AuditRecord(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out
