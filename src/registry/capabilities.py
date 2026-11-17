"""Роли задач и наблюдаемые возможности исполнителей.

Всё, что здесь объявлено — декларация УДОБСТВА (под какие задачи исполнитель
настроен), а не измеренная метрика и не обещание качества. Стоимость и квота
здесь НЕ объявляются: их факт даёт probe() (см. probe.py), а если не даёт —
честно unknown.

Роли:
  architecture  — сложные архитектурные решения: предпочтение Codex;
  review        — ревью кода/плана: предпочтение Codex (независимое ревью);
  coding        — написание/правка кода: предпочтение OpenCode (DeepSeek);
  cheap_test    — дешёвые/простые проверки: Gemini, только если доступен;
  general       — всё остальное: по дефолтному ранжированию.
"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    architecture = "architecture"
    review = "review"
    coding = "coding"
    cheap_test = "cheap_test"
    general = "general"


KNOWN_ROLES: frozenset[str] = frozenset(r.value for r in Role)


class Capability(str, Enum):
    """Наблюдаемые возможности, которыми маршрутизатор может руководствоваться."""

    code_edit = "code_edit"      # умеет править файлы в worktree
    long_context = "long_context"  # заявлен большой контекст
    headless = "headless"        # есть неинтерактивный режим
    local_auth = "local_auth"    # авторизация уже хранится локально


# Статическая карта «исполнитель -> возможности». Это свойство адаптеров,
# а не бесконечный каталог: одна строка на адаптер.
CAPABILITIES: dict[str, frozenset[Capability]] = {
    "codex": frozenset({Capability.code_edit, Capability.long_context,
                        Capability.headless, Capability.local_auth}),
    "opencode": frozenset({Capability.code_edit, Capability.long_context,
                           Capability.headless, Capability.local_auth}),
    # Gemini объявлен только после установки+авторизации; см. registry.probe.
    "gemini": frozenset({Capability.headless, Capability.local_auth}),
}


def guaranteed(executor: str, caps) -> bool:
    """Все затребованные capability покрыты объявленной картой исполнителя."""
    imeya = CAPABILITIES.get(executor, frozenset())
    return all(Capability(c) in imeya for c in caps)
