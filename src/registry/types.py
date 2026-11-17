"""Единые факты о пробах и решениях выбора исполнителя.

Только то, что можно установить фактически: наличие бинаря, файл авторизации,
явный offline-флаг. Никаких оценок стоимости/квоты «на глаз»: где неизвестно —
unknown, и unknown НЕ равно available (см. ProbeReport.usable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class QuotaState(str, Enum):
    """Состояние квоты исполнителя на момент пробы.

    known_ok   — явное положительное подтверждение (для prepaid API DeepSeek);
    known_exhausted — факт исчерпания (получен из last_error/адаптера);
    unknown    — мы НЕ знаем; это не «есть квота» (решение принимается с
                 учётом capability/политики, а не выдаётся за «свободно»).
    n/a        — у исполнителя нет понятия квоты (локальный mock).
    """

    known_ok = "known_ok"
    known_exhausted = "known_exhausted"
    unknown = "unknown"
    not_applicable = "n/a"


class Availability(str, Enum):
    available = "available"          # бинарь + авторизация подтверждены локально
    not_installed = "not_installed"  # нет бинаря
    not_authorized = "not_authorized"  # бинарь есть, файла авторизации нет
    offline = "offline"              # явный флаг offline
    error = "error"                  # проба упала (не значит offline)


@dataclass(frozen=True)
class ProbeReport:
    """Фактическая проба одного исполнителя. Все поля — факты, не догадки."""

    tag: str
    installed: bool
    authorized: bool
    available: bool
    quota_state: QuotaState = QuotaState.unknown
    last_error: str | None = None
    last_error_class: str | None = None
    status: Availability = Availability.error
    offline: bool = False
    binary: str | None = None
    detail: str = ""
    checked_at: float = 0.0

    @property
    def usable(self) -> bool:
        """Можно ли прямо сейчас запускать шаг.

        available == True И квота не known_exhausted. unknown-квота допускает
        запуск только если доступность подтверждена; unknown сам по себе НЕ
        делает исполнителя доступным, если available == False.
        """
        return self.available and self.quota_state != QuotaState.known_exhausted

    def as_audit(self) -> dict:
        return {
            "tag": self.tag,
            "installed": self.installed,
            "authorized": self.authorized,
            "available": self.available,
            "quota_state": self.quota_state.value,
            "status": self.status.value,
            "last_error": self.last_error,
            "last_error_class": self.last_error_class,
            "detail": self.detail,
        }


class Role(str, Enum):
    """Роль задачи в терминах capability routing."""

    coding = "coding"          # правка кода в репозитории
    architecture = "architecture"  # сложная архитектура / проектирование
    review = "review"          # ревью чужого кода/диффа
    simple_test = "simple_test"  # дешёвые тесты/короткие проверки
    generic = "generic"        # всё остальное без специфики


class SelectionStatus(str, Enum):
    selected = "selected"
    none = "none"              # нет подходящего исполнителя — НЕ запускаем
    exhausted = "exhausted"    # достигнут лимит запусков/кандидатов
    offline = "offline"        # все кандидаты offline


@dataclass
class Budget:
    """Локальный бюджет попыток маршрутизации для ОДНОГО шага.

    Не денежный: денежный бюджет в оркестраторе — отдельная сущность. Здесь
    только сколько запусков/переключений разрешено на этот вызов select().
    """

    max_launches: int = 3      # жёсткий потолок запусков на шаг (совпадает с policy)
    max_candidates: int = 3    # максимум кандидатов рассматривать
    attempts_used: int = 0     # сколько запусков уже было (монотонно растёт)


@dataclass
class Selection:
    """Итог маршрутизации для одного шага."""

    status: SelectionStatus
    tag: str | None = None
    role: Role = Role.generic
    reason: str = ""
    candidates: list[str] = field(default_factory=list)   # порядок предпочтения
    rejected: list[dict] = field(default_factory=list)    # {tag, why}
    launch_index: int = 1

    def as_audit(self) -> dict:
        return {
            "status": self.status.value,
            "tag": self.tag,
            "role": self.role.value,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "rejected": list(self.rejected),
            "launch_index": self.launch_index,
        }
