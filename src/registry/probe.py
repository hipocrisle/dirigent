"""Фактическое состояние исполнителя из одного probe().

Источники — только локальные и бесплатные:
  * `shutil.which(binary)` — установлен ли CLI;
  * наличие файла авторизации (адаптер знает свой путь) — авторизован ли;
  * доступность = установлен AND авторизован AND последняя ошибка не auth;
  * последняя ошибка — из журнала последнего запуска (персистится в audit);
  * квота — из последней фактической ошибки класса quota. Значения квоты
    НЕ вычисляются: `used`, `limit`, `remaining` всегда None, если источник
    их не сообщил явным числом (сейчас не сообщает никто) — это и есть
    честный «unknown». «unknown» НИКОГДА не трактуется как «доступно»:
    это отдельное значение QuotaState.unknown со смыслом «не знаем».
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class QuotaState(str, Enum):
    unknown = "unknown"      # никто не сообщил факт — не значит «есть запас»
    ok = "ok"                # последний запуск завершился без признаков квоты
    exhausted = "exhausted"  # последняя ошибка класса quota (reset_at если известен)


@dataclass(frozen=True)
class ProbeResult:
    executor: str
    installed: bool
    authenticated: bool
    available: bool
    quota_state: QuotaState = QuotaState.unknown
    quota_reset_at: float | None = None   # unix-time сброса (None = unknown)
    # Фактические значения квоты; None == «источник не сообщил».
    quota_used: float | None = None
    quota_limit: float | None = None
    last_error_class: str | None = None
    last_error_text: str = ""
    last_error_at: float | None = None
    probed_at: float = field(default_factory=time.time)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quota_state"] = self.quota_state.value
        return d

    def quota_blocks(self, now: float | None = None) -> bool:
        """Квота реально мешает запуску прямо сейчас (а не когда-то раньше)."""
        if self.quota_state is not QuotaState.exhausted:
            return False
        if self.quota_reset_at is None:
            return True          # unknown reset -> консервативно блокируем
        return (now if now is not None else time.time()) < self.quota_reset_at


# Поля, которые персистятся как «последний запуск» между перезапусками демона.
LAST_RUN_FIELDS = ("last_error_class", "last_error_text", "last_error_at",
                   "quota_state", "quota_reset_at")


def last_run_state_from_audit(records) -> tuple[dict[str, dict[str, Any]], float | None]:
    """executor -> поля последнего ПРОГОНА из AuditRecord (порядок не важен).

    Возвращает (map, последний_ts_записи). Решение по квоте должно пережить
    рестарт демона, иначе «unknown» затрёт известное «exhausted» — это ложное
    обогащение, а не безопасное восстановление.
    """
    out: dict[str, dict[str, Any]] = {}
    seen_at: dict[str, float] = {}
    last_ts: float | None = None
    for rec in records:
        last_ts = rec.at if last_ts is None else max(last_ts, rec.at)
        ex = getattr(rec, "executor", None)
        if not ex:
            continue
        data = rec.data or {}
        at = data.get("at")
        if at is None:
            continue
        at = float(at)
        if at >= seen_at.get(ex, -1.0):
            seen_at[ex] = at
            out[ex] = {k: data.get(k) for k in LAST_RUN_FIELDS if k in data}
    return out, last_ts
