"""Мост registry ↔ существующие адаптеры (без второго CLI).

Не создаёт новых процессов и не дублирует ``codex`` через ``opencode``:
все запуски идут через ``src.executors`` (codex.py / opencode.py / mock).
Здесь только:

* ``classify_step_result`` — связать факт ``StepResult`` с состоянием реестра
  (quota -> note_quota, network -> note_network);
* ``preferred_executor_for_result`` — какой следующий кандидат выбрать после
  quota/network-сбоя (для failover в планировщике).

Вызывается из планировщика ПОСЛЕ ``ex.run()``, чтобы следующий тик уже видел
факт исчерпания квоты/сети.
"""
from __future__ import annotations

from ..models import ErrorClass, StepResult
from .core import ExecutorRegistry


def record_failure(reg: ExecutorRegistry, tag: str, result: StepResult) -> None:
    """Зафиксировать в реестре факт квоты/сети по результату адаптера.

    Только факты: известное время сброса — карантин до него, иначе cooldown.
    Другие классы ошибок в реестре не меняют доступность.
    """
    if result.ok:
        return
    if result.error_class is ErrorClass.quota:
        reg.note_quota(tag, reset_at=result.quota_reset_at)
    elif result.error_class is ErrorClass.network:
        reg.note_network(tag)


def failover_decision(reg: ExecutorRegistry, key: str, tag: str,
                      result: StepResult) -> bool:
    """True — шаг можно перезапустить на ДРУГОМ исполнителе в рамках лимита.

    Вызывается в планировщике после фиксации ошибки. Падаем в failover только
    на quota/network (а не на runtime/unknown: там проблема в задаче, а не в
    исполнителе). Лимит запусков — общий (MAX_LAUNCHES=3).
    """
    if result.ok:
        return False
    if result.error_class not in (ErrorClass.quota, ErrorClass.network):
        return False
    record_failure(reg, tag, result)
    if not reg.can_launch(key):
        return False
    # пометить pin как устаревший — следующий select() выберет другого
    reg._pinned.pop(key, None)
    return True
