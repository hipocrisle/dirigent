"""Машина состояний шагов и заказов.

Переходы — только через can_transition/apply: произвольный скачок состояния
запрещён (спека §6.2). Терминальные: completed/failed/cancelled.
"""
from __future__ import annotations

from enum import Enum


class StepState(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    waiting_resource = "waiting_resource"
    waiting_approval = "waiting_approval"
    waiting_quota = "waiting_quota"
    retry_scheduled = "retry_scheduled"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class OrderState(str, Enum):
    new = "new"
    active = "active"
    paused = "paused"
    waiting_human = "waiting_human"
    done = "done"
    error = "error"
    cancelled = "cancelled"


TERMINAL_STEPS = {StepState.completed, StepState.failed, StepState.cancelled}

_ALLOWED: set[tuple[StepState, StepState]] = {
    (StepState.pending, StepState.ready),
    (StepState.pending, StepState.cancelled),      # каскад от отмены/провала зависимости
    (StepState.ready, StepState.running),
    (StepState.ready, StepState.waiting_resource),
    (StepState.ready, StepState.waiting_approval),  # approval.mode=before
    (StepState.running, StepState.waiting_approval),  # результат есть, ждём приёмку (after)
    (StepState.ready, StepState.cancelled),
    (StepState.waiting_resource, StepState.ready),
    (StepState.waiting_approval, StepState.ready),  # approved (mode=before)
    (StepState.waiting_approval, StepState.completed),  # принят результат (mode=after)
    (StepState.waiting_approval, StepState.cancelled),  # rejected/timeout-reject
    (StepState.running, StepState.completed),
    (StepState.running, StepState.failed),
    (StepState.running, StepState.waiting_quota),
    (StepState.running, StepState.retry_scheduled),
    (StepState.running, StepState.cancelled),
    (StepState.waiting_quota, StepState.ready),     # квота восстановлена
    (StepState.waiting_quota, StepState.cancelled),
    (StepState.retry_scheduled, StepState.ready),   # backoff истёк
    (StepState.retry_scheduled, StepState.cancelled),
    (StepState.failed, StepState.retry_scheduled),  # редко: ручной перезапуск из TUI
}

_ORDER_ALLOWED: set[tuple[OrderState, OrderState]] = {
    (OrderState.new, OrderState.active),
    (OrderState.new, OrderState.cancelled),
    (OrderState.active, OrderState.paused),
    (OrderState.paused, OrderState.active),
    (OrderState.active, OrderState.waiting_human),
    (OrderState.active, OrderState.done),
    (OrderState.active, OrderState.error),
    (OrderState.active, OrderState.cancelled),
    (OrderState.waiting_human, OrderState.done),    # апрув закрыл последний шаг
    (OrderState.waiting_human, OrderState.error),
    (OrderState.waiting_human, OrderState.active),  # ответ получен
    (OrderState.waiting_human, OrderState.cancelled),
    (OrderState.error, OrderState.active),          # ручной рестарт после эскалации
    (OrderState.error, OrderState.cancelled),
}


def can_step(a: StepState, b: StepState) -> bool:
    if a == b:
        return False
    if a in TERMINAL_STEPS:
        return False                      # из терминала — только новый шаг, не переход
    return (a, b) in _ALLOWED


def can_order(a: OrderState, b: OrderState) -> bool:
    if a == b:
        return False
    return (a, b) in _ORDER_ALLOWED
