"""Ядро executor registry / capability routing.

Публичный контракт:

* ``ExecutorRegistry.probe()`` — фактический снимок по всем исполнителям:
  installed / authorized / available / offline / quota_state / last_error.
  Никаких внешних платных API; только локальные данные + явные флаги.
* ``ExecutorRegistry.select(role, policy, budget, capabilities)`` — выбор
  подходящего исполнителя, идемпотентный для одного шага (key отдаёт тот же
  исполнитель, пока условия не изменились). Не более ``MAX_LAUNCHES`` запусков
  на шаг и не более ``MAX_PARALLEL`` одновременных выборов.
* ``ExecutorRegistry.note_quota(...)`` / ``note_network(...)`` — фиксация
  факта исчерпания квоты/сбоя сети от адаптера, чтобы следующий select()
  сделал failover.

Принципы (совпадают с PLAN):
  AD-1 — тут нет LLM-вызовов; только учёт фактов.
  unknown quota != available: исполнитель с ``available=False`` не выбирается,
  даже если квота unknown. known_exhausted — не выбирается до сброса/сброса флага.
  max 3 launches на шаг: ``Budget.max_launches`` жёстко клампится к
  ``policy.MAX_STEP_LAUNCHES``; исчерпание -> status=exhausted.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..policy import MAX_PARALLEL_EXECUTORS, MAX_STEP_LAUNCHES
from .policy import (
    KNOWN_CAPABILITIES,
    lacks_capabilities,
    order_for_role,
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

log = logging.getLogger("dirigent.registry")

MAX_LAUNCHES = MAX_STEP_LAUNCHES       # 3
MAX_PARALLEL = MAX_PARALLEL_EXECUTORS   # 2


@dataclass
class RoutingAudit:
    """Запись аудита: кто/почему выбран (или почему нет)."""

    at: float
    key: str | None
    role: str
    selection: Selection
    probes: dict[str, dict] = field(default_factory=dict)
    attempt: int = 1

    def as_dict(self) -> dict:
        return {
            "at": self.at,
            "key": self.key,
            "role": self.role,
            "attempt": self.attempt,
            "selection": self.selection.as_audit(),
            "probes": self.probes,
        }


class ExecutorRegistry:
    """Реестр исполнителей + маршрутизатор. Один экземпляр на процесс демона."""

    def __init__(self, probers: dict | None = None, max_parallel: int | None = None,
                 clock=time.time):
        if probers is None:
            from .probes import CodexProber, GeminiProber, OpencodeProber
            probers = {
                "codex": CodexProber(),
                "opencode": OpencodeProber(),
                "gemini": GeminiProber(),
            }
        self.probers = dict(probers)
        self.max_parallel = max_parallel or MAX_PARALLEL
        self._clock = clock

        # Последний снимок проб (кэшируется, чтобы select не дёргал проберы каждый раз).
        self._reports: dict[str, ProbeReport] = {}
        # Исполнители, отправленные в карантин после quota/network.
        self._quota_cool_until: dict[str, float] = {}
        self._network_cool_until: dict[str, float] = {}
        # Журнал маршрутизации (in-memory; интеграция пишет в db.event).
        self.audit_log: list[RoutingAudit] = []
        # Идемпотентность: key -> (tag, содержимое выбора).
        self._pinned: dict[str, Selection] = {}
        # Счётчики попыток на key (для max 3 launches).
        self._launch_count: dict[str, int] = {}
        # Ограничитель параллелизма 2 для одновременно выбранных запусков.
        self._inflight = 0
        self._inflight_lock = asyncio.Lock()
        self._select_lock = asyncio.Lock()

    # ── пробы ──────────────────────────────────────────────────
    async def probe(self) -> dict[str, ProbeReport]:
        """Опросить все исполнители параллельно. Возвращает снимок фактов."""
        tags = list(self.probers)
        results = await asyncio.gather(*(self.probers[t].probe() for t in tags),
                                       return_exceptions=True)
        out: dict[str, ProbeReport] = {}
        for tag, res in zip(tags, results):
            if isinstance(res, BaseException):
                out[tag] = ProbeReport(
                    tag=tag, installed=False, authorized=False, available=False,
                    quota_state=QuotaState.unknown, status=Availability.error,
                    detail=f"probe error: {res!r}", checked_at=self._clock())
            else:
                out[tag] = res
        self._reports = out
        return dict(out)

    def snapshot(self) -> dict[str, ProbeReport]:
        """Последний известный снимок без новой пробы (может быть пустым)."""
        return dict(self._reports)

    def _quota_state(self, rep: ProbeReport) -> QuotaState:
        now = self._clock()
        if self._quota_cool_until.get(rep.tag, 0) > now:
            return QuotaState.known_exhausted
        return rep.quota_state

    def _is_cooling(self, tag: str) -> tuple[bool, str]:
        now = self._clock()
        if self._quota_cool_until.get(tag, 0) > now:
            return True, "quota"
        if self._network_cool_until.get(tag, 0) > now:
            return True, "network"
        return False, ""

    # ── фиксация фактов от адаптера ────────────────────────────
    def note_quota(self, tag: str, reset_at: float | None = None,
                   cooldown_seconds: float = 300.0) -> None:
        """Квота исчерпана. reset_at известен — карантин до него; иначе cooldown."""
        now = self._clock()
        until = reset_at if reset_at and reset_at > now else now + cooldown_seconds
        self._quota_cool_until[tag] = until

    def note_network(self, tag: str, cooldown_seconds: float = 30.0) -> None:
        self._network_cool_until[tag] = self._clock() + cooldown_seconds

    def clear_cooldown(self, tag: str) -> None:
        self._quota_cool_until.pop(tag, None)
        self._network_cool_until.pop(tag, None)

    def set_report(self, tag: str, rep: ProbeReport) -> None:
        """Обновить снимок для tag (для адаптеров/тестов)."""
        self._reports[tag] = rep

    # ── параллелизм ────────────────────────────────────────────
    @property
    def inflight(self) -> int:
        return self._inflight

    def free_slots(self) -> int:
        return max(0, self.max_parallel - self._inflight)

    def acquire_slot(self) -> bool:
        """Занять слот запуска (не блокирует). False -> ждать следующего тика."""
        if self._inflight >= self.max_parallel:
            return False
        self._inflight += 1
        return True

    def release_slot(self) -> None:
        if self._inflight > 0:
            self._inflight -= 1

    # ── идемпотентность ───────────────────────────────────────
    def _attempts(self, key: str | None, budget: Budget) -> int:
        used = budget.attempts_used
        if key:
            used = max(used, self._launch_count.get(key, 0))
        return used

    # ── выбор ──────────────────────────────────────────────────
    async def select(self, role: Role | str = Role.generic,
                     policy: dict | None = None,
                     budget: Budget | None = None,
                     capabilities: set[str] | frozenset[str] | None = None,
                     *, key: str | None = None,
                     refresh: bool = False) -> Selection:
        """Выбрать исполнителя под роль.

        role: Role или строка (coding/architecture/review/simple_test/generic).
        policy: переопределения {"preference": [tags], "exclude": [tags],
                "force_tag": tag}.
        budget: локальный бюджет запусков (клампится к MAX_LAUNCHES).
        capabilities: требуемые возможности (code/tests/cheap/...).
        key: идемпотентный ключ шага, например "ord/step". Тот же key, пока
             условия не изменились, отдаёт того же исполнителя и не тратит
             попытку повторно; после quota/network — failover к следующему.
        refresh: переопросить проберы (по умолчанию используется снимок).
        """
        role = Role(role) if not isinstance(role, Role) else role
        policy = policy or {}
        budget = budget or Budget()
        caps = frozenset(capabilities or ())

        async with self._select_lock:
            if refresh or not self._reports:
                await self.probe()
            return await self._select_locked(role, policy, budget, caps, key)

    async def _select_locked(self, role: Role, policy: dict, budget: Budget,
                             caps: frozenset[str], key: str | None) -> Selection:
        max_launches = min(budget.max_launches, MAX_LAUNCHES)
        used = self._attempts(key, budget)

        # жёсткий стоп ДО идемпотентности: лимит запусков не обходится pin'ом
        if used >= max_launches:
            sel = Selection(status=SelectionStatus.exhausted, role=role,
                            reason=f"лимит запусков исчерпан ({used}/{max_launches})",
                            launch_index=used)
            self._audit(key, role, sel, self._reports, used)
            return sel

        # идемпотентность: уже выбранный под key исполнитель остаётся, если всё ещё usable
        if key and key in self._pinned and not _policy_changed(policy, caps):
            prev = self._pinned[key]
            rep = self._reports.get(prev.tag or "")
            if rep and self._is_usable(rep) and not self._is_cooling(rep.tag)[0]:
                sel = Selection(status=SelectionStatus.selected, tag=prev.tag,
                                role=role, reason="idempotent: pinned по key",
                                candidates=[prev.tag], launch_index=used or 1)
                self._audit(key, role, sel, self._reports, used or 1)
                return sel
            # условия поменялись — убираем pin, продолжаем как failover
            self._pinned.pop(key, None)

        pref = list(policy.get("preference") or order_for_role(role))
        forced = policy.get("force_tag")
        if forced:
            pref = [forced] + [t for t in pref if t != forced]
        excluded = set(policy.get("exclude") or [])

        rejected: list[dict] = []
        chosen: str | None = None

        for tag in pref:
            if tag in excluded:
                rejected.append({"tag": tag, "why": "excluded by policy"})
                continue
            rep = self._reports.get(tag)
            if rep is None:
                rejected.append({"tag": tag, "why": "нет пробы (не зарегистрирован)"})
                continue
            cooling, why = self._is_cooling(tag)
            quota = self._quota_state(rep)
            if cooling:
                rejected.append({"tag": tag, "why": f"{why}: карантин"})
                continue
            if not rep.available:
                rejected.append({"tag": tag, "why": f"недоступен ({rep.status.value})"})
                continue
            if quota is QuotaState.known_exhausted:
                rejected.append({"tag": tag, "why": "квота исчерпана"})
                continue
            missing = lacks_capabilities(tag, caps)
            if missing:
                rejected.append({"tag": tag, "why": f"нет возможностей: {', '.join(missing)}"})
                continue
            chosen = tag
            break

        if chosen is None:
            all_offline = bool(self._reports) and all(
                r.status is Availability.offline for r in self._reports.values()) \
                if self._reports else False
            status = SelectionStatus.offline if all_offline else SelectionStatus.none
            reason = ("все исполнители offline" if all_offline
                      else "нет подходящего исполнителя по фактам проб")
            sel = Selection(status=status, role=role, reason=reason,
                            rejected=rejected, launch_index=used + 1)
            self._audit(key, role, sel, self._reports, used)
            return sel

        launch_index = used + 1
        reason = _reason_for(role, chosen, pref, rejected, budget)
        sel = Selection(status=SelectionStatus.selected, tag=chosen, role=role,
                        reason=reason, candidates=pref, rejected=rejected,
                        launch_index=launch_index)
        if key:
            self._pinned[key] = Selection(status=SelectionStatus.selected,
                                          tag=chosen, role=role, reason=reason,
                                          launch_index=launch_index)
            # фиксируем попытку только когда синхронно выбираем на текущем тике;
            # адаптер подтверждает фактический запуск через note_launch
        self._audit(key, role, sel, self._reports, launch_index)
        return sel

    def _is_usable(self, rep: ProbeReport) -> bool:
        return rep.available and self._quota_state(rep) != QuotaState.known_exhausted

    # ── учёт фактического запуска ─────────────────────────────
    def note_launch(self, key: str | None, tag: str) -> int:
        """Адаптер/планировщик вызывает после фактического старта. Возвращает счётчик."""
        if key is None:
            return 0
        self._launch_count[key] = self._launch_count.get(key, 0) + 1
        return self._launch_count[key]

    def launches_for(self, key: str) -> int:
        return self._launch_count.get(key, 0)

    def can_launch(self, key: str) -> bool:
        return self._launch_count.get(key, 0) < MAX_LAUNCHES

    def reset_key(self, key: str) -> None:
        """Снять pin/счётчики (используется при приёмке или ручном retry)."""
        self._pinned.pop(key, None)
        self._launch_count.pop(key, None)

    # ── аудит ─────────────────────────────────────────────────
    def _audit(self, key: str | None, role: Role, sel: Selection,
               reports: dict[str, ProbeReport], attempt: int) -> None:
        entry = RoutingAudit(at=self._clock(), key=key, role=role.value,
                             selection=sel, attempt=attempt,
                             probes={t: r.as_audit() for t, r in reports.items()})
        self.audit_log.append(entry)
        log.info("registry.select role=%s tag=%s status=%s reason=%s",
                 sel.role.value, sel.tag, sel.status.value, sel.reason)

    def last_audit(self, key: str | None = None) -> RoutingAudit | None:
        pool = self.audit_log
        if key is not None:
            pool = [a for a in pool if a.key == key]
        return pool[-1] if pool else None


def _policy_changed(policy: dict, caps: frozenset[str]) -> bool:
    return bool(policy.get("preference") or policy.get("force_tag")
                or policy.get("exclude") or caps)


def _reason_for(role: Role, chosen: str, pref: list[str], rejected: list[dict],
                budget: Budget) -> str:
    skipped = [f"{r['tag']}({r['why']})" for r in rejected]
    base = f"role={role.value}: выбран {chosen} (порядок: {', '.join(pref)})"
    if skipped:
        base += f"; отклонены: {'; '.join(skipped)}"
    return base


def read_capabilities(raw: list[str] | set[str] | None) -> frozenset[str]:
    """Валидатор capability-тегов шага; неизвестный игнорируется, чтобы не блокировать."""
    if not raw:
        return frozenset()
    return frozenset(c for c in raw if c in KNOWN_CAPABILITIES)
