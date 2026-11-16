"""Дирижёр: модели заказа и шага.

Схема — единая точка входа: YAML заказа валидируется здесь же.
Контракт шага machine-checkable: acceptance — данные для чекеров, не текст.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


class ErrorClass(str, Enum):
    ok = "ok"
    quota = "quota"        # исчерпана квота исполнителя (Codex и т.п.)
    auth = "auth"          # проблема авторизации
    network = "network"    # сеть/соединение
    timeout = "timeout"    # превышен таймаут
    resource = "resource"  # ресурс недоступен (homepc off, GPU занят)
    runtime = "runtime"    # исполнитель отработал с ошибкой
    cancelled = "cancelled"
    unknown = "unknown"


class RetryPolicy(BaseModel):
    max_attempts: int = 2
    backoff_seconds: float = 5.0


class ApprovalPolicy(BaseModel):
    mode: Literal["none", "before", "after"] = "none"
    timeout_seconds: float = 3600.0
    # default при молчании: reject (опасные) или wait (ждать дальше).
    # Молчание НИКОГДА не означает согласие.
    default_action: Literal["reject", "wait"] = "wait"


class StepSpec(BaseModel):
    id: str
    type: str                              # тег исполнителя: mock.* | codex | opencode | shell
    executor: str | None = None            # предпочтительный ресурс-исполнитель
    depends_on: list[str] = []
    prompt: str = ""                       # задача для LLM-исполнителя или команда
    inputs: dict[str, Any] = {}
    outputs: dict[str, str] = {}           # имя артефакта -> относительный путь
    acceptance: dict[str, Any] = {}        # machine-checkable критерии (см. checkers)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    timeout_seconds: float = 1800.0
    budget_usd: float | None = None


class OrderSpec(BaseModel):
    id: str | None = None
    goal: str
    priority: int = 0
    budget_max_usd: float | None = None
    repo: str | None = None                # путь к репо задачи (для coding-шагов)
    constraints: dict[str, Any] = {}
    steps: list[StepSpec]

    @model_validator(mode="after")
    def _check(self) -> "OrderSpec":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        idset = set(ids)
        for s in self.steps:
            for d in s.depends_on:
                if d not in idset:
                    raise ValueError(f"step {s.id}: unknown dependency {d}")
        return self


class StepResult(BaseModel):
    """Единый формат результата исполнителя (факт, не оценка)."""
    ok: bool
    error_class: ErrorClass = ErrorClass.ok
    error_text: str = ""
    artifacts: dict[str, str] = {}         # имя -> абсолютный путь
    usage: dict[str, float] = {}           # tokens_in/out, usd — факт из API
    quota_reset_at: float | None = None    # если квота и время известно (иначе None=unknown)
    run_id: str | None = None              # внешний id для status()/cancel()
    log_tail: str = ""                     # хвост stdout исполнителя (наблюдаемость)
