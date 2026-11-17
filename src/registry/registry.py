"""Реестр исполнителей с маршрутизацией по ролям и бюджета запусков.

Два независимых слоя:
  1. `probe_all()` — фактическое состояние каждого исполнителя (см. probe.py);
  2. `select(role, policy, budget, capabilities)` — детерминированный выбор
     подходящего исполнителя и ФорМАЛЬНАЯ причина выбора.

Гарантии:
  * quota unknown != available: неизвестная квота не освобождает от
    ограничений — учитывается порядком ранжирования и консервативной
    проверкой `quota_blocks()`; «exhausted» с неизвестным ресетом блокирует
    исполнителя целиком;
  * предпочтения по ролям (см. ROLE_PREFERENCE) — для сложной архитектуры и
    ревью Codex, для кодинга OpenCode, для дешёвых тестов Gemini — но только
    ЕСЛИ он реально доступен; иначе — следующий по порядку и это видно в reason;
  * failover только на классах, которые имеет смысл перепробовать
    (quota/network/auth/timeout/resource); runtime/unknown — терминальны;
  * бюджет запусков шага — MAX_LAUNCHES=3 (по умолчанию), включая первый;
  * идемпотентность — ключ launch_key: повтор с тем же ключом не даёт нового
    запуска, если результат известен, и помечает retry=True иначе;
  * параллелизм — MAX_PARALLEL=2 суммарно по всем исполнителям.

Никаких выдуманных чисел квоты/стоимости: если факта нет — None/unknown.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..executors import registry as default_executors
from ..models import ErrorClass, StepResult, StepSpec
from ..policy import MAX_PARALLEL_EXECUTORS, MAX_STEP_LAUNCHES
from .audit import AuditLog, AuditRecord
from .capabilities import CAPABILITIES, KNOWN_ROLES, Capability, Role, guaranteed
from .gemini import GeminiExecutor, auth_status
from .probe import (
    LAST_RUN_FIELDS, ProbeResult, QuotaState, last_run_state_from_audit,
)

log = logging.getLogger("dirigent.registry")

MAX_LAUNCHES: int = MAX_STEP_LAUNCHES          # 3 (policy.py)
MAX_PARALLEL: int = MAX_PARALLEL_EXECUTORS      # 2 (policy.py)
DEFAULT_PROBE_TTL: float = 60.0               # секунд между бесплатными probe

# Порядок = приоритет. Первый доступный получает роль; если его нет — честный
# второй вариант, и это отражается в reason.
ROLE_PREFERENCE: dict[Role, tuple[str, ...]] = {
    Role.architecture: ("codex", "opencode", "gemini"),
    Role.review: ("codex", "opencode", "gemini"),
    Role.coding: ("opencode", "codex"),          # gemini не умеет править файлы
    Role.cheap_test: ("gemini", "opencode", "codex"),
    Role.general: ("codex", "opencode", "gemini"),
}

# Классы ошибок, после которых имеет смысл попробовать другого исполнителя.
FAILOVER_CLASSES: frozenset[ErrorClass] = frozenset({
    ErrorClass.quota, ErrorClass.network, ErrorClass.auth,
    ErrorClass.timeout, ErrorClass.resource,
})

# Исполнители, которые обязаны работать в git worktree (правят файлы).
WORKTREE_TAGS: frozenset[str] = frozenset({"codex", "opencode"})

# Ошибки, после которых исполнитель считается «сломавшимся» до probe/auth.
_STICKY_BAD = frozenset({ErrorClass.auth})


class SelectionError(RuntimeError):
    """Не удалось выбрать исполнителя. `tried` — кто и почему отброшен."""

    def __init__(self, reason: str, tried: dict[str, str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.tried = tried or {}


@dataclass(frozen=True)
class Selection:
    executor: str
    role: Role
    reason: str
    policy: str
    budget_left: int
    preferred: str          # первый по ROLE_PREFERENCE (даже если недоступен)
    probe: ProbeResult | None = None
    candidates: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "role": self.role.value if isinstance(self.role, Role) else self.role,
            "reason": self.reason,
            "policy": self.policy,
            "budget_left": self.budget_left,
            "preferred": self.preferred,
            "candidates": dict(self.candidates),
            "probe": self.probe.to_dict() if self.probe else None,
        }


@dataclass(frozen=True)
class LaunchAttempt:
    attempt: int
    executor: str
    result: StepResult
    reason: str             # почему именно этот запуск случился
    at: float


@dataclass(frozen=True)
class LaunchOutcome:
    ok: bool
    result: StepResult
    executor: str | None
    attempts: tuple[LaunchAttempt, ...]
    launch_key: str
    deduplicated: bool = False   # True => новый запуск НЕ делался, это replay
    launches_used: int = 0

    @property
    def terminal(self) -> bool:
        return self.ok or self.result.error_class not in FAILOVER_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "executor": self.executor,
            "launch_key": self.launch_key,
            "deduplicated": self.deduplicated,
            "launches_used": self.launches_used,
            "error_class": self.result.error_class.value,
            "error_text": self.result.error_text,
            "attempts": [
                {"attempt": a.attempt, "executor": a.executor,
                 "reason": a.reason, "error_class": a.result.error_class.value,
                 "error_text": a.result.error_text}
                for a in self.attempts
            ],
        }


@dataclass
class _CachedOutcome:
    """Идемпотентный кэш запусков по launch_key (в памяти демона)."""

    state: str                     # running | done
    result: StepResult | None = None
    executor: str | None = None
    attempts: tuple[LaunchAttempt, ...] = ()
    future: asyncio.Future | None = None


class ExecutorRegistry:
    """Фактический probe + маршрутизация + ограниченный failover.

    Параметры:
      executors   — tag -> адаптер (по умолчанию reuse src.executors.registry;
                    gemini добавляется только если реально установлен+авторизован);
      audit       — AuditLog (JSONL); reason по каждому решению;
      probe_ttl   — сколько секунд кэшировать probe (probe дешёвый, но не бьём
                    диск каждого вызова);
      max_launches/max_parallel — защитные потолки, не могут быть подняты выше
                    политики (policy.MAX_STEP_LAUNCHES / MAX_PARALLEL_EXECUTORS).
    """

    def __init__(self, executors: Mapping[str, Any] | None = None, *,
                 audit: AuditLog | None = None,
                 probe_ttl: float = DEFAULT_PROBE_TTL,
                 max_launches: int = MAX_LAUNCHES,
                 max_parallel: int = MAX_PARALLEL,
                 home: str | None = None,
                 clock=time.time):
        self.executors: dict[str, Any] = dict(executors) if executors is not None \
            else self._default_executors(home)
        self.audit = audit or AuditLog()
        self.probe_ttl = probe_ttl
        self.max_launches = min(int(max_launches), MAX_LAUNCHES)
        self.max_parallel = min(int(max_parallel), MAX_PARALLEL)
        self._clock = clock
        self._probe_cache: dict[str, ProbeResult] = {}
        self._probe_at: dict[str, float] = {}
        # Свежий факт последнего запуска в этом процессе (см. _merge_last_run)
        self._run_state: dict[str, dict[str, Any]] = {}
        # Идемпотентность: launch_key -> последнее известное состояние
        self._launch_cache: dict[str, _CachedOutcome] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(self.max_parallel)
        self._running = 0               # счётчик активных исполнителей
        self._peak = 0

    # ── построение по умолчанию ───────────────────────────────
    @staticmethod
    def _default_executors(home: str | None) -> dict[str, Any]:
        ex = dict(default_executors)
        gem = GeminiExecutor(home=home or None)
        if shutil.which(gem.binary):
            # добавляем даже неавторизованного: probe честно скажет authenticated=False
            ex.setdefault("gemini", gem)
        return ex

    # ── probe ─────────────────────────────────────────────────
    async def probe(self, executor: str, *, force: bool = False) -> ProbeResult:
        teper = self._clock()   # тестовые часы влияют только на TTL кэша
        if not force:
            cached = self._probe_cache.get(executor)
            if cached and teper - self._probe_at.get(executor, 0) < self.probe_ttl:
                return cached
        adapter = self.executors.get(executor)
        if adapter is None:
            pr = ProbeResult(executor=executor, installed=False,
                             authenticated=False, available=False,
                             notes="адаптер не сконфигурирован")
        else:
            pr = await self._probe_one(executor, adapter)
        self._probe_cache[executor] = pr
        self._probe_at[executor] = teper
        try:
            self.audit.append("probe", executor=executor, reason=pr.notes,
                              at=pr.probed_at, probe=pr.to_dict())
        except OSError as e:   # аудит не должен валить probe
            log.warning("audit probe failed: %s", e)
        return pr

    async def _probe_one(self, tag: str, adapter: Any) -> ProbeResult:
        try:
            installed_probe = getattr(adapter, "is_installed", None)
            if callable(installed_probe):
                installed = bool(await installed_probe())
            else:
                # адаптеры Дирижёра не выставляют .binary для mock: у них
                # имя бинаря совпадает с тегом, только если он реально есть
                binar = getattr(adapter, "binary", None)
                if binar is not None:
                    installed = bool(shutil.which(binar))
                else:
                    # без объявленного бинаря судим по available() адаптера
                    installed = True
        except Exception:
            installed = False

        if isinstance(adapter, GeminiExecutor):
            # только НАСТОЯЩИЙ GeminiExecutor: у фейков авторизация — их факт
            ok, why = auth_status(getattr(adapter, "home", None) or None,
                                  _env_of(adapter))
            authenticated = bool(installed and ok)
            notes = why
        else:
            authenticated = False
            notes = "auth проверяется адаптером через available()"
            if installed:
                try:
                    avail = bool(await adapter.available())
                    authenticated = avail
                    notes = "binary+локальный auth/конфиг"
                except Exception as e:
                    notes = f"available() упал: {e!r}"

        last = self._merge_last_run(tag)
        last_class = last.get("last_error_class")
        try:
            quota_state = QuotaState(last.get("quota_state") or "unknown")
        except ValueError:
            quota_state = QuotaState.unknown
        reset = last.get("quota_reset_at")

        # Локальные факты: установлен/авторизован. Последняя ошибка auth
        # делает исполнителя недоступным для автоматики (нужен ручной вход),
        # пока не будет НОВОГО успешного запуска: успешный запуск пишет
        # last_error_class=ok и этим снимает флаг.
        available = installed and authenticated
        notes_avail = ""
        if available and last_class == ErrorClass.auth.value:
            available = False
            notes_avail = "последняя ошибка — auth, нужен ручной вход"
        if notes_avail:
            notes = notes_avail

        pr = ProbeResult(
            executor=tag,
            installed=installed,
            authenticated=authenticated,
            available=available,
            quota_state=quota_state,
            quota_reset_at=reset,
            last_error_class=last_class,
            last_error_text=last.get("last_error_text") or "",
            last_error_at=last.get("last_error_at"),
            probed_at=_wall(),
            notes=notes,
        )
        # «unknown quota» НЕ является «available» автоматически: после того как
        # видели exhausted, требование к сбросу сохраняется; но restart демона не
        # должен терять знание — журнал переживает его.
        return pr

    def _audit_state(self, tag: str) -> dict[str, dict[str, Any]]:
        """Последний ФАКТ запуска каждого исполнителя (переживает рестарт).

        Порядок строк в журнале произволен (асинхронные корутины), поэтому
        берём максимум по `data.at`, а не по position в файле.
        """
        try:
            records: Iterable[AuditRecord] = self.audit.read_tail(limit=1000)
            return last_run_state_from_audit(records)[0]
        except Exception:
            return {}

    def _last_run(self, tag: str) -> dict[str, Any]:
        return self._audit_state(tag).get(tag, {})

    def _merge_last_run(self, tag: str) -> dict[str, Any]:
        """Свежайший факт запуска: прямое значение (текущий процесс) важнее журнала.

        Без этого старый файл журнала (из предыдущего процесса/прогона тестов)
        мог бы перебить актуальное in-memory состояние — и наоборот. Сравниваем
        по timestamp записи.
        """
        disk = self._last_run(tag)
        mem = self._run_state.get(tag, {})
        if not disk:
            return mem
        if not mem:
            return disk
        return mem if float(mem.get("at") or 0) >= float(disk.get("at") or 0) else disk

    def _invalidate_probe(self, *tags: str) -> None:
        for tag in tags:
            self._probe_cache.pop(tag, None)

    async def probe_all(self, *, force: bool = False) -> dict[str, ProbeResult]:
        names = sorted(self.executors)
        out = await asyncio.gather(*(self.probe(n, force=force) for n in names))
        return dict(zip(names, out))

    # ── выбор исполнителя ─────────────────────────────────────
    async def select(self, role: str | Role = Role.general, *,
                     policy: str | None = None,
                     budget: int | None = None,
                     capabilities: Sequence[str | Capability] = (),
                     attempts_used: int = 0,
                     audit: bool = True,
                     order_id: str | None = None,
                     step_id: str | None = None,
                     exclude: Iterable[str] = ()) -> Selection:
        """Выбрать исполнителя для роли.

        budget — сколько ещё ЛЮБЫХ запусков шага допустимо (None = max_launches).
        attempts_used — уже потрачено запусков шага; budget_left уменьшается.
        exclude — исполнители, которые уже провалились на этом шаге.
        policy — имя политики (`default|prefer:<tag>|role`), только для аудита.
        """
        rt_role = role if isinstance(role, Role) else _coerce_role(role)
        budget_left = self._budget_left(budget, attempts_used)
        excluded = set(exclude)
        caps = [Capability(c) for c in capabilities]
        preferred = ROLE_PREFERENCE[rt_role][0]
        order = ROLE_PREFERENCE[rt_role]

        candidates: dict[str, str] = {}
        chosen: Selection | None = None

        if budget_left <= 0:
            raise SelectionError(
                f"budget exhausted: launches {attempts_used} >= {self.max_launches}",
                {"__budget__": "no launches left"})

        probes = {ex: await self.probe(ex) for ex in order if ex in self.executors}
        now = _wall()
        for ex in order:
            pr = probes.get(ex)
            verdict = self._reject_reason(pr, excluded, caps, now, preferred, rt_role, budget_left)
            if verdict is not None:
                candidates[ex] = verdict
                continue
            candidates[ex] = "ok"
            if policy and policy.startswith("prefer:"):
                tag = policy.split(":", 1)[1]
                if ex != tag:
                    candidates[ex] = f"policy {policy} требует {tag}"
                    continue
            reason = self._reason_for(ex, rt_role, preferred, pr, caps)
            chosen = Selection(executor=ex, role=rt_role, reason=reason,
                               policy=policy or "default", budget_left=budget_left,
                               preferred=preferred, probe=pr, candidates=candidates)
            break

        if chosen is None:
            tried = ", ".join(f"{k}: {v}" for k, v in candidates.items()) or "нет"
            if audit:
                self._audit("select", None, role=rt_role.value,
                            reason="no available executor",
                            order_id=order_id, step_id=step_id,
                            data={"tried": candidates, "budget_left": budget_left,
                                  "capabilities": [c.value for c in caps],
                                  "policy": policy or "default"})
            raise SelectionError(f"no available executor ({tried})", candidates)

        if audit:
            self._audit("select", chosen.executor, role=rt_role.value,
                        reason=chosen.reason, order_id=order_id, step_id=step_id,
                        data={"preferred": preferred, "candidates": candidates,
                              "budget_left": budget_left,
                              "capabilities": [c.value for c in caps],
                              "policy": chosen.policy,
                              "probe": chosen.probe.to_dict() if chosen.probe else None})
        return chosen

    def _budget_left(self, budget: int | None, attempts_used: int) -> int:
        if budget is None:
            return self.max_launches - max(0, attempts_used)
        return min(int(budget), self.max_launches) - max(0, attempts_used)

    @staticmethod
    def _reason_for(ex: str, role: Role, preferred: str, pr: ProbeResult | None,
                    caps: list[Capability]) -> str:
        base = f"role={role.value}: выбран {ex}"
        if ex == preferred:
            base += " (первый в предпочтении роли)"
        else:
            base += f" (предпочтение {preferred} отброшено — см. candidates)"
        if pr is not None:
            base += f"; probe: quota={pr.quota_state.value}"
            if pr.quota_state is QuotaState.exhausted and pr.quota_reset_at:
                base += f" (reset_at={time.strftime('%Y-%m-%d %H:%M', time.localtime(pr.quota_reset_at))})"
            if pr.quota_state is QuotaState.unknown:
                base += " (неизвестна — не считаем запасом)"
        if caps:
            base += f"; capabilities={','.join(c.value for c in caps)}"
        if pr is not None and pr.quota_state is QuotaState.exhausted \
                and not pr.quota_blocks():
            base += " (последний шанс бюджета: reset прошёл, факта нет)"
        return base

    def _reject_reason(self, pr: ProbeResult | None, excluded: set[str],
                       caps: list[Capability], now: float,
                       preferred: str, role: Role,
                       budget_left: int = 1) -> str | None:
        ex = pr.executor if pr else "?"
        if pr is None:
            return "нет адаптера"
        if ex in excluded:
            return "уже провалился на этом шаге (failover)"
        if not pr.installed:
            return "CLI не установлен"
        if not pr.authenticated:
            return f"не авторизован ({pr.notes})"
        if pr.last_error_class == ErrorClass.auth.value:
            return "последняя ошибка — auth (требует ручного входа)"
        if not pr.available:
            return "недоступен по probe"
        if caps and not guaranteed(ex, caps):
            miss = [c.value for c in caps if c not in CAPABILITIES.get(ex, frozenset())]
            return f"нет capability: {','.join(miss)}"
        if pr.quota_blocks(now):
            when = "unknown reset" if pr.quota_reset_at is None else \
                time.strftime('%H:%M', time.localtime(pr.quota_reset_at))
            return f"квота исчерпана ({when})"
        if pr.quota_state is QuotaState.exhausted:
            # reset уже прошёл, но нового факта запуска нет: осторожно, только
            # если иначе запусков не хватит на достойную альтернативу. Это
            # не «доступен», а «из двух зол — меньшее»; видно в reason.
            if budget_left <= 1:
                return None
            return "квота: reset прошёл, но новых фактов нет"
        return None

    # ── запуск с failover ─────────────────────────────────────
    async def launch(self, step: StepSpec, *,
                     role: str | Role = Role.general,
                     policy: str | None = None,
                     budget: int | None = None,
                     capabilities: Sequence[str | Capability] = (),
                     lock: Any = None,
                     preacquired: bool = False,
                     workdir: str | None = None,
                     launch_key: str | None = None) -> LaunchOutcome:
        """Запустить шаг с маршрутизацией и ограниченным failover.

        `lock` — threading/asyncio-совместимый lock, уже взятый вызывающим
        (одна попытка = один атомарный запуск); `preacquired=True` означает,
        что вызывающий уже взял lock. Если lock не передан — берём внутренний
        asyncio.Lock по launch_key.
        """
        key = launch_key or _default_launch_key(step)
        cached = self._launch_cache.get(key)
        if cached is not None and cached.state == "done":
            return LaunchOutcome(ok=bool(cached.result and cached.result.ok),
                                 result=cached.result or StepResult(ok=False),
                                 executor=cached.executor,
                                 attempts=cached.attempts, launch_key=key,
                                 deduplicated=True, launches_used=len(cached.attempts))
        if cached is not None and cached.state == "running" and cached.future is not None:
            # тот же ключ уже исполняется в другом корутине: ждём его завершения
            res = await asyncio.shield(cached.future)
            return res

        lock = lock or self._locks.setdefault(key, asyncio.Lock())
        if not preacquired:
            await _acquire(lock)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._launch_cache[key] = _CachedOutcome(state="running", future=fut)
        try:
            out = await self._launch_locked(
                step, key=key, role=role, policy=policy, budget=budget,
                capabilities=capabilities, workdir=workdir)
            self._launch_cache[key] = _CachedOutcome(
                state="done", result=out.result, executor=out.executor,
                attempts=out.attempts)
            if not fut.done():
                fut.set_result(out)
            return out
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e) if isinstance(e, Exception) else fut.cancel()
            self._launch_cache.pop(key, None)
            raise
        finally:
            if not preacquired:
                _release(lock)

    async def _launch_locked(self, step: StepSpec, *, key: str, role, policy,
                             budget, capabilities, workdir) -> LaunchOutcome:
        rt_role = role if isinstance(role, Role) else _coerce_role(role)
        attempts: list[LaunchAttempt] = []
        tried_log: list[str] = []
        while len(attempts) < self.max_launches:
            excluded = {a.executor for a in attempts}
            if self._budget_left(budget, len(attempts)) <= 0:
                break
            try:
                sel = await self.select(rt_role, policy=policy, budget=budget,
                                        capabilities=capabilities,
                                        attempts_used=len(attempts),
                                        order_id=step.id, step_id=step.id,
                                        exclude=excluded)
            except SelectionError as e:
                prev = attempts[-1].result if attempts else None
                res = StepResult(
                    ok=False,
                    error_class=(prev.error_class if prev else ErrorClass.resource),
                    error_text=f"registry: {e.reason}",
                    log_tail="\n".join(tried_log))
                self._audit("exhausted", None, role=rt_role.value,
                            reason=e.reason, order_id=step.id, step_id=step.id,
                            data={"tried": e.tried, "quota_state_after": {
                                ex: pr.quota_state.value
                                for ex, pr in (await self.probe_all()).items()}})
                return LaunchOutcome(ok=False, result=res, executor=None,
                                     attempts=tuple(attempts), launch_key=key,
                                     launches_used=len(attempts))
            ex = sel.executor
            reason = (sel.reason if not attempts else
                      f"failover после {attempts[-1].result.error_class.value}: "
                      + sel.reason)
            self._audit("launch", ex, role=rt_role.value, reason=reason,
                        order_id=step.id, step_id=step.id, attempt=len(attempts) + 1,
                        data={"launch_key": key, "budget_left": sel.budget_left})
            res = await self._run_attempt(ex, step, workdir)
            attempts.append(LaunchAttempt(attempt=len(attempts) + 1, executor=ex,
                                          result=res, reason=reason, at=_wall()))
            tried_log.append(f"[{len(attempts)}] {ex}: {res.error_class.value}"
                             f" {res.error_text[:200]}")
            if res.ok:
                return LaunchOutcome(ok=True, result=res, executor=ex,
                                     attempts=tuple(attempts), launch_key=key,
                                     launches_used=len(attempts))
            if res.error_class not in FAILOVER_CLASSES:
                return LaunchOutcome(ok=False, result=res, executor=ex,
                                     attempts=tuple(attempts), launch_key=key,
                                     launches_used=len(attempts))
            # failover: следующий виток select исключит провалившегося
            if len(attempts) >= self.max_launches:
                self._audit("exhausted", None, role=rt_role.value,
                            reason=f"max launches {self.max_launches} reached",
                            order_id=step.id, step_id=step.id,
                            data={"tried": tried_log})
                break

        last = attempts[-1].result if attempts else StepResult(
            ok=False, error_class=ErrorClass.resource, error_text="no launches")
        res = StepResult(ok=False, error_class=last.error_class,
                         error_text=last.error_text,
                         quota_reset_at=last.quota_reset_at,
                         usage=last.usage,
                         log_tail="\n".join(tried_log))
        return LaunchOutcome(ok=False, result=res,
                             executor=attempts[-1].executor if attempts else None,
                             attempts=tuple(attempts), launch_key=key,
                             launches_used=len(attempts))

    async def _run_attempt(self, ex: str, step: StepSpec,
                           workdir: str | None) -> StepResult:
        adapter = self.executors.get(ex)
        if adapter is None:
            return StepResult(ok=False, error_class=ErrorClass.resource,
                              error_text=f"no adapter {ex}")
        start = _monotonic()
        if self._running >= self.max_parallel:
            # Вызывать launch без внешнего слота не надо: держим собственный
            # семафор, чтобы даже вразнобой вызванные корутины не превышали потолок
            log.debug("registry parallel limit wait (ex=%s)", ex)
        async with self._slots:
            self._running += 1
            self._peak = max(self._peak, self._running)
            try:
                try:
                    res = await adapter.run(step, workdir=workdir)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    res = StepResult(ok=False, error_class=ErrorClass.unknown,
                                     error_text=f"executor {ex} crashed: {e!r}")
            finally:
                self._running -= 1
        elapsed = _monotonic() - start
        # Запись фактического последнего запуска: она и питает следующий probe
        # (единственный способ узнать квоту без платных проб). `at` пишем в
        # формате, который понимает probe.last_run_state_from_audit.
        now = _wall()
        stat = {"is_run": True, "at": now, "elapsed": round(elapsed, 3),
                "last_error_class": res.error_class.value,
                "last_error_text": (res.error_text or "")[:2000],
                "last_error_at": now,
                "quota_state": _quota_state_after(res),
                "quota_reset_at": res.quota_reset_at}
        self._run_state[ex] = dict(stat)
        self._audit("launch_result", ex, reason=res.error_class.value,
                    order_id=step.id, step_id=step.id, data=stat)
        # Инвалидируем кэш probe: состояние изменил факт запуска. Меняем
        # ВСЕМ, а не только ex: выбор следующего шага видит свежее состояние.
        self._probe_cache.clear()
        return res

    # ── идемпотентность / диагностика ─────────────────────────
    def forget(self, launch_key: str) -> None:
        """Сбросить кэш результата (напр. ручной ретрай через CLI/TUI)."""
        self._launch_cache.pop(launch_key, None)

    @property
    def peak_parallel(self) -> int:
        return self._peak

    def _audit(self, kind: str, executor: str | None, **kw) -> None:
        try:
            self.audit.append(kind, executor=executor, **kw)
        except OSError as e:
            log.warning("audit %s failed: %s", kind, e)


# ── вспомогательное ────────────────────────────────────────────
def _coerce_role(role: str) -> Role:
    try:
        return Role(role)
    except ValueError:
        if role not in KNOWN_ROLES:
            log.debug("unknown role %r -> general", role)
        return Role.general


def _wall() -> float:
    """Настоящее время для решений о квоте. `at` в журнале — тоже wall-clock.

    `self._clock` — тестовые часы только для TTL probe-кэша, чтобы тест мог
    «перемотать» кэш и не влиял на смысл reset_at (который в test/real всегда
    от часов, которые выбрал вызывающий).
    """
    return time.time()


def _monotonic() -> float:
    return time.monotonic()


def _quota_state_after(res: StepResult) -> str:
    if res.error_class is ErrorClass.quota:
        return QuotaState.exhausted.value
    if res.ok:
        return QuotaState.ok.value
    # network/auth/timeout не дают оснований говорить что-то о квоте
    return QuotaState.unknown.value


def _env_of(adapter: Any) -> dict | None:
    getter = getattr(adapter, "_environ", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _default_launch_key(step: StepSpec) -> str:
    return f"{step.id}:{step.type}:{step.prompt[:120]}"


async def _acquire(lock) -> None:
    if isinstance(lock, asyncio.Lock):
        await lock.acquire()
    else:  # threading.Lock, взятый в executor'е
        await asyncio.get_running_loop().run_in_executor(None, lock.acquire)


def _release(lock) -> None:
    if isinstance(lock, asyncio.Lock):
        lock.release()
    else:
        lock.release()
