"""Обёртка над Scheduler, включающая исполнителя через registry.

Правка существующего src/scheduler.py делает ровно одну вещь: если шаг
явно помечен (`type` начинается с "registry" или `executor=="registry"`),
исполнение уходит в `RegistryScheduler._run_via_registry`; иначе поведение
байт-в-байт прежнее. Всё остальное (worktree, апрувы, приёмка, мердж,
живой `ex.available()` для waiting_resource) переиспользуется как есть.

Семантика результата registry-запуска для машины состояний:
  ok                                      -> completed (как обычно);
  quota + reset_at unknown                -> FAILED (unknown != available:
                                             не обещаем «вернётся само»);
  quota + известный reset_at в будущем    -> waiting_quota (перепроверка по времени);
  network/timeout + попытки исчерпаны     -> failed;
  resource (нет ни одного исполнителя)    -> waiting_resource (ждать, не жечь).

Использование в scheduler.py (единственная правка):
    from .registry.scheduler import RegistrySchedulerMixin
    class Scheduler(RegistrySchedulerMixin): ...

и в начале `_run_inner`, после получения `spec`:
    if self._is_registry_step(spec):
        return await self._run_via_registry(oid, sid, spec, repo)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..models import ErrorClass, StepResult, StepSpec
from ..scheduler import Scheduler
from ..states import StepState
from .capabilities import Role
from .registry import ExecutorRegistry, LaunchOutcome

log = logging.getLogger("dirigent.registry.scheduler")

REGISTRY_TAGS_PREFIX = "registry"


def is_registry_step(spec: StepSpec) -> bool:
    return (spec.type.split(".")[0] == REGISTRY_TAGS_PREFIX
            or spec.executor == REGISTRY_TAGS_PREFIX)


def role_of(spec: StepSpec) -> Role:
    """Роль из `inputs.role`; default — general. Неизвестная роль -> general."""
    raw = (spec.inputs or {}).get("role") or Role.general.value
    try:
        return Role(str(raw))
    except ValueError:
        log.warning("step %s: unknown role %r -> general", spec.id, raw)
        return Role.general


class RegistrySchedulerMixin:
    """Примешивается к Scheduler. Registry создаётся лениво.

    Позволяет использовать ExecutorRegistry без переписывания ядра. Регистрирует
    адаптеры registry (codex/opencode/gemini) в self.exec, чтобы единый
    `available()`-путь не расходился с тем, что видит probe.
    """

    registry: ExecutorRegistry | None = None

    def _get_registry(self) -> ExecutorRegistry:
        reg = getattr(self, "_registry_obj", None)
        if reg is None:
            reg = ExecutorRegistry(getattr(self, "exec", None))
            for tag, adapter in reg.executors.items():
                getattr(self, "exec", {}).setdefault(tag, adapter)
            self._registry_obj = reg
        return reg

    # хуки ниже переопределяют одноимённые методы Scheduler ---------------
    def _is_registry_step(self, spec: StepSpec) -> bool:   # noqa: D401
        return is_registry_step(spec)

    def _plan_step(self, spec: StepSpec) -> tuple[str, Any, bool]:
        """Справочный маршрут БЕЗ побочных эффектов (для CLI/TUI «куда пойдёт»).

        Возвращает (путь, исполнитель, registry?). Registry-исполнитель зависит
        от роли и probe, поэтому здесь отдаётся только признак registry-пути.
        """
        if is_registry_step(spec):
            return (REGISTRY_TAGS_PREFIX, None, True)
        tag = spec.type.split(".")[0]
        ex = getattr(self, "exec", {}).get(tag)
        if ex is None and getattr(self, "_registry_obj", None) is not None:
            return (REGISTRY_TAGS_PREFIX, None, True)
        return (tag, ex, False)

    async def _run_via_registry(self, oid: str, sid: str, spec: StepSpec,
                                repo: str | None) -> None:   # noqa: D401
        db: Any = self.db
        reg = self._get_registry()
        role = role_of(spec)
        cap = (spec.inputs or {}).get("capabilities") or ()
        if isinstance(cap, str):
            cap = [cap]
        budget = (spec.inputs or {}).get("budget")

        # probe перед работой: все недоступны -> ждём ресурс, не жжём бюджет
        probes = await reg.probe_all(force=True)
        if not any(p.available for p in probes.values()):
            # Уходим в waiting_resource НАДЁЖНО: running->waiting_resource нет в
            # таблице переходов ядра (известное расхождение: в src/scheduler.py
            # waiting_resource ставится из ready), поэтому registry-путь сначала
            # возвращает шаг в ready (свой lease, переход разрешён), затем — ждать.
            await db.transition_step(oid, sid, StepState.ready, force=True,
                                     lease_owner=None, lease_until=None,
                                     backoff_until=0.0)
            await db.transition_step(oid, sid, StepState.waiting_resource,
                                     lease_owner=None, lease_until=None)
            await db.event("step.waiting_resource", oid, sid,
                           {"tag": REGISTRY_TAGS_PREFIX, "role": role.value,
                            "probes": {k: v.to_dict() for k, v in probes.items()}})
            return

        # worktree как у ядра для всех исполнителей, которые правят файлы
        workdir = repo
        wt = wt_branch = None
        if repo:
            from ..workspace import ensure_worktree
            try:
                wt, wt_branch = ensure_worktree(repo, oid, sid)
                workdir = wt
                await db.event("step.worktree", oid, sid,
                               {"path": wt, "branch": wt_branch})
            except Exception as e:
                await db.event("step.worktree_failed", oid, sid, {"err": str(e)[:200]})
                await db.finish_step(oid, sid, StepResult(
                    ok=False, error_class=ErrorClass.runtime,
                    error_text=f"worktree: {str(e)[:200]}"))
                return

        if spec.approval.mode == "before":
            if not await self._ask_and_wait(
                    oid, sid,
                    f"Шаг {sid} требует подтверждения перед запуском: {spec.prompt}",
                    spec):
                return

        hb = asyncio.create_task(self._heartbeat(oid, sid))
        try:
            out: LaunchOutcome = await reg.launch(
                spec, role=role, capabilities=cap, budget=budget,
                workdir=workdir, launch_key=f"{oid}:{sid}")
        finally:
            hb.cancel()
        result = out.result

        # acceptance + merge: переиспользуем те же проверки, что и ядро
        if result.ok and spec.acceptance.get("checks"):
            from ..checkers import run_acceptance
            chk = run_acceptance(spec, workdir, result.artifacts)
            if not chk.ok:
                result = StepResult(ok=False, error_class=ErrorClass.runtime,
                                    error_text=f"acceptance failed: {chk.details}",
                                    usage=result.usage, log_tail=result.log_tail)
        if result.ok and wt:
            from ..workspace import cleanup_worktree, commit_all, merge_to_integrate
            commit_all(wt, f"dirigent: {oid}/{sid}")
            ok, detail = merge_to_integrate(repo, wt_branch)
            result.artifacts["branch"] = wt_branch
            result.artifacts["merge"] = detail
            await db.event("step.merge", oid, sid,
                           {"branch": wt_branch, "ok": ok, "detail": detail[:200]})
            if ok:
                cleanup_worktree(repo, wt, wt_branch)
        if result.ok and spec.approval.mode == "after":
            await db.transition_step(oid, sid, StepState.waiting_approval,
                                     lease_owner=None, lease_until=None,
                                     result_json=result.model_dump_json())
            await self._open_approval(
                oid, sid,
                f"Шаг {sid} выполнен. Принять результат? ({list(result.artifacts)})",
                spec, artifacts=result.artifacts)
            return

        # Маппинг класса ошибки на машину состояний registry-специфика
        if result.ok:
            await db.finish_step(oid, sid, result)
        elif result.error_class is ErrorClass.resource:
            # все кандидаты пропали между probe и выбором: не терминально —
            # вернуть в очередь на ресурс (переход через ready, как выше)
            await db.transition_step(oid, sid, StepState.ready, force=True,
                                     lease_owner=None, lease_until=None,
                                     backoff_until=0.0)
            await db.transition_step(oid, sid, StepState.waiting_resource,
                                     lease_owner=None, lease_until=None)
            await db.event("step.waiting_resource", oid, sid,
                           {"tag": REGISTRY_TAGS_PREFIX, "role": role.value,
                            "reason": "no executor at select time"})
        elif result.error_class is ErrorClass.quota and result.quota_reset_at is None:
            # unknown quota != available: не оставляем в waiting_quota,
            # иначе ядро будет бесконечно перепроверять без знания времени
            result = StepResult(
                ok=False, error_class=ErrorClass.runtime,
                error_text=(result.error_text or "") +
                " [registry: quota exhausted, reset unknown -> terminal]",
                log_tail=result.log_tail)
            await db.finish_step(oid, sid, result)
        else:
            await db.finish_step(oid, sid, result)


class RegistryScheduler(RegistrySchedulerMixin, Scheduler):
    """Готовая подмена для того, кто хочет без правки ядра.

    Не используется демоном, пока Kimi не переключит `cli.py`/`api.py`;
    сохранена, чтобы интеграция была одной строкой.
    """
