"""Правила маршрутизации по роли: кто предпочтителен и почему.

Это политика, а НЕ оценка стоимости. Порядок предпочтения — явный, чтобы
audit мог объяснить «почему выбран X». Gemini участвует только там, где
допустим дешёвый исполнитель (простые задачи/тесты), и только если реально
доступен: недоступный Gemini не блокирует coding.

Ключевые правила задания:
* Codex — предпочтителен для сложной архитектуры и review;
* OpenCode (DeepSeek) — предпочтителен для coding;
* Gemini — дешёвые тесты и простые задачи, только если доступен;
* generic — сначала что есть по факту, но по консервативному порядку.
"""
from __future__ import annotations

from .types import Role

# Порядок предпочтения по роли. Первый доступный и есть выбранный.
ROLE_PREFERENCE: dict[Role, tuple[str, ...]] = {
    Role.architecture: ("codex", "opencode", "gemini"),
    Role.review: ("codex", "opencode", "gemini"),
    Role.coding: ("opencode", "codex", "gemini"),
    Role.simple_test: ("gemini", "opencode", "codex"),
    Role.cheap_test: ("gemini", "opencode", "codex"),
    Role.generic: ("opencode", "codex", "gemini"),
}

# Роли, для которых исполнитель обязан «уметь код» (coding-capable).
CODING_ROLES = {Role.coding, Role.architecture, Role.review}

# Исполнители, которые считаются coding-capable (правят файлы в worktree).
CODING_CAPABLE = frozenset({"codex", "opencode"})

# Легитимные значения capability-тегов, которые может запросить шаг.
KNOWN_CAPABILITIES = frozenset({
    "code", "architecture", "review", "tests", "cheap", "long_context",
})

# Какие возможности заявляет исполнитель (фактическая возможность, не квота).
EXECUTOR_CAPABILITIES: dict[str, frozenset[str]] = {
    "codex": frozenset({"code", "architecture", "review", "long_context"}),
    "opencode": frozenset({"code", "tests", "cheap", "long_context"}),
    "gemini": frozenset({"tests", "cheap"}),
}


def order_for_role(role: Role) -> tuple[str, ...]:
    return ROLE_PREFERENCE.get(role, ROLE_PREFERENCE[Role.generic])


def lacks_capabilities(tag: str, capabilities: set[str] | frozenset[str]) -> list[str]:
    """Каких запрошенных возможностей у исполнителя нет (пусто = подходит)."""
    have = EXECUTOR_CAPABILITIES.get(tag, frozenset())
    return sorted(set(capabilities) - set(have))


def is_coding_capable(tag: str) -> bool:
    return tag in CODING_CAPABLE
