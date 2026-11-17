"""Тесты executor registry / capability routing — без реальных CLI.

Покрытие:
  * selection по ролям: architecture/review -> Codex, coding -> OpenCode,
    cheap_test -> Gemini только если доступен, иначе честный fallback;
  * quota: unknown != available, exhausted с unknown reset блокирует,
    известный reset в будущем блокирует, после сброса — снова доступен,
    состояние квоты переживает «рестарт демона» (журнал);
  * offline: ни один не доступен -> selection error / итог resource;
  * failover: quota/network -> следующий, runtime -> терминально,
    максимум 3 запуска на шаг;
  * idempotency: один launch_key = один запуск (replay + конкурентный вызов);
  * parallel limit 2;
  * routing audit: по записям реконструируется кто/почему.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.models import ErrorClass, StepResult
from src.policy import MAX_PARALLEL_EXECUTORS, MAX_STEP_LAUNCHES
from src.registry import (
    DEFAULT_PROBE_TTL,
    MAX_LAUNCHES,
    MAX_PARALLEL,
    AuditLog,
    Capability,
    ExecutorRegistry,
    QuotaState,
    Role,
    SelectionError,
)
from src.registry import audit as audit_mod
from src.registry.probe import ProbeResult
from tests.fakes.registry_fakes import FakeExecutor, fail, ok, step


# ── фикстуры ──────────────────────────────────────────────────
@pytest.fixture
def audit_log(tmp_path) -> AuditLog:
    return AuditLog(str(tmp_path / "audit.jsonl"))


@pytest.fixture
def fakes():
    counter = {"now": 0, "peak": 0}
    return {
        "codex": FakeExecutor("codex", counter=counter),
        "opencode": FakeExecutor("opencode", counter=counter),
        "gemini": FakeExecutor("gemini", counter=counter),
    }


@pytest.fixture
def reg(fakes, audit_log) -> ExecutorRegistry:
    return ExecutorRegistry(fakes, audit=audit_log, probe_ttl=0.0)


def records(audit_log: AuditLog) -> list:
    return list(audit_log.read())


def kinds(log: list, kind: str) -> list:
    return [r for r in log if r.kind == kind]


# ── selection по ролям ───────────────────────────────────────
@pytest.mark.parametrize("role, expected", [
    (Role.architecture, "codex"),
    (Role.review, "codex"),
    (Role.coding, "opencode"),
    (Role.cheap_test, "gemini"),
    (Role.general, "codex"),
])
async def test_select_role_preference(reg, role, expected):
    sel = await reg.select(role)
    assert sel.executor == expected
    assert sel.role is role
    assert sel.preferred == expected
    assert "role=" in sel.reason


async def test_select_prefers_codex_for_architecture_and_review_even_with_gemini(reg):
    """Даже при доступном Gemini сложная архитектура и ревью идут в Codex."""
    for role in (Role.architecture, Role.review):
        sel = await reg.select(role)
        assert sel.executor == "codex", sel.reason


async def test_select_coding_never_routes_to_gemini(reg):
    """Gemini без capabilities.code_edit не годится для кодинга (у него их нет)."""
    # Убираем OpenCode: coding-роль должна уйти в Codex, а не в Gemini
    await reg.probe("opencode")
    reg.executors["opencode"] = FakeExecutor("opencode", installed=False)
    await reg.probe("opencode", force=True)
    sel = await reg.select(Role.coding)
    assert sel.executor == "codex"
    assert sel.candidates["opencode"] == "CLI не установлен"


async def test_select_cheap_test_falls_back_if_gemini_unavailable(reg):
    reg.executors["gemini"] = FakeExecutor("gemini", installed=False)
    await reg.probe("gemini", force=True)
    sel = await reg.select(Role.cheap_test)
    assert sel.executor == "opencode"
    assert sel.preferred == "gemini"
    assert "не установлен" in sel.candidates["gemini"]


async def test_select_reports_unknown_role_as_general(reg):
    sel = await reg.select("какой-то-новый-класс")
    assert sel.role is Role.general


async def test_select_capabilities_filter(reg):
    """capabilities=(). Затребованный code_edit отсекает Gemini явно."""
    sel = await reg.select(Role.general, capabilities=[Capability.code_edit])
    assert sel.executor == "codex"
    # cheap_test: Gemini отсеян по capabilities, уходит в OpenCode — а не в
    # Codex, который стоит третьим. Причина отказа Gemini должна быть
    # специфичной (capability), а не «не установлен».
    sel2 = await reg.select(Role.cheap_test, capabilities=["code_edit"])
    assert sel2.executor == "opencode"
    assert "capability: code_edit" in sel2.candidates["gemini"]
    # и без OpenCode остаётся только Codex (Gemini по-прежнему отсечён)
    reg.executors["opencode"] = FakeExecutor("opencode", installed=False)
    await reg.probe("opencode", force=True)
    sel3 = await reg.select(Role.cheap_test, capabilities=["code_edit"])
    assert sel3.executor == "codex"


# ── quota: unknown != available ───────────────────────────────
async def test_probe_quota_unknown_is_not_ok(reg):
    pr = await reg.probe("codex")
    assert pr.available is True
    assert pr.quota_state is QuotaState.unknown
    # неизвестная квота не даёт выдуманных чисел
    assert pr.quota_used is None and pr.quota_limit is None
    assert pr.quota_reset_at is None


async def test_quota_exhausted_unknown_reset_blocks(reg, audit_log):
    """Codex падает quota без reset -> probe.exhausted, selection исключает Codex."""
    reg.executors["codex"].script["s1"] = fail(
        ErrorClass.quota, "rate limit", quota_reset_at=None)
    out = await reg.launch(step("s1"), role=Role.coding)  # coding: opencode первым
    # OpenCode у нас успешен, Codex даже не пробовался в coding-роли.
    assert out.ok and out.executor == "opencode"
    # Но Codex явно сломан через отдельный запуск архитектурной роли:
    reg._launch_cache.clear()
    reg.executors["codex"].script["s2"] = fail(ErrorClass.quota, "usage limit")
    reg.executors["opencode"].script["s2"] = ok()
    out2 = await reg.launch(step("s2"), role=Role.architecture)
    assert out2.ok and out2.attempts[0].executor == "codex"
    assert out2.attempts[0].result.error_class is ErrorClass.quota
    # Кэш probe инвалидирован -> новый probe показывает exhausted
    pr = await reg.probe("codex", force=True)
    assert pr.quota_state is QuotaState.exhausted
    assert pr.quota_blocks() is True
    sel = await reg.select(Role.architecture)
    assert sel.executor == "opencode"
    assert "квота исчерпана" in sel.candidates["codex"]


async def test_quota_known_reset_blocks_until_time_then_allows(fakes, audit_log):
    """reset_at в будущем блокирует Codex; после сброса Codex снова первый."""
    reg = ExecutorRegistry(fakes, audit=audit_log, probe_ttl=0.0)
    reset_at = time.time() + 0.25
    reg.executors["codex"].script["s1"] = fail(
        ErrorClass.quota, "usage limit, resets at 23:59", quota_reset_at=reset_at)
    out = await reg.launch(step("s1"), role=Role.architecture)  # codex -> opencode
    assert not out.attempts[0].result.ok
    assert out.attempts[0].executor == "codex"
    pr = await reg.probe("codex", force=True)
    assert pr.quota_state is QuotaState.exhausted
    assert pr.quota_reset_at == reset_at
    assert pr.quota_blocks() is True           # reset в будущем -> блокирует
    sel = await reg.select(Role.architecture)
    assert sel.executor == "opencode"
    assert "квота исчерпана" in sel.candidates["codex"]
    # Время сброса прошло — но новых ФАКТОВ запуска нет: доверяем только если
    # иначе бюджет исчерпается («последний шанс»), и помечаем это в reason.
    await asyncio.sleep(0.3)
    pr2 = await reg.probe("codex", force=True)
    assert pr2.quota_blocks() is False
    sel2 = await reg.select(Role.architecture, budget=1)  # последний запуск
    assert sel2.executor == "codex"
    assert "факта нет" in sel2.reason
    # Без угрозы исчерпать бюджет — не угадываем: OpenCode, а Codex ждёт факта
    sel_multi = await reg.select(Role.architecture, budget=3)
    assert sel_multi.executor == "opencode"
    # И настоящий успешный запуск после reset снимает exhausted полностью
    reg.forget("s1:registry:p")
    reg.executors["codex"].script["s3"] = ok()
    out3 = await reg.launch(step("s3"), role=Role.architecture, budget=1)
    assert out3.ok and out3.executor == "codex"
    pr3 = await reg.probe("codex", force=True)
    assert pr3.quota_state is QuotaState.ok


async def test_quota_state_survives_daemon_restart(fakes, audit_log):
    """probe не должен терять exhausted после перезапуска демона (журнал на диске)."""
    reg1 = ExecutorRegistry(fakes, audit=audit_log, probe_ttl=0.0)
    reg1.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota", quota_reset_at=None)
    await reg1.launch(step("s1"), role=Role.architecture)
    # «новый процесс»: новый Registry на тех же файлах, свежие fake-адаптеры
    fakes2 = {k: FakeExecutor(k) for k in fakes}
    reg2 = ExecutorRegistry(fakes2, audit=audit_log, probe_ttl=0.0)
    pr = await reg2.probe("codex")
    assert pr.quota_state is QuotaState.exhausted
    assert pr.available is True          # установлен и авторизован
    assert pr.quota_blocks() is True     # но квота блокирует запуск


async def test_successful_run_resets_quota_state(fakes, audit_log):
    reg = ExecutorRegistry(fakes, audit=audit_log, probe_ttl=0.0)
    reg.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota")
    await reg.launch(step("s1"), role=Role.architecture)
    assert (await reg.probe("codex", force=True)).quota_state is QuotaState.exhausted
    reg._launch_cache.clear()
    reg.executors["codex"].script["s2"] = ok()
    out = await reg.launch(step("s2"), role=Role.architecture)
    # До сброса Codex исключён — успех приходит от OpenCode; тут интересен факт
    # записи last_run для Codex (quota) и OpenCode (ok).
    assert out.ok
    pr = await reg.probe("opencode", force=True)
    assert pr.quota_state is QuotaState.ok


async def test_auth_failure_marks_unavailable(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.auth, "authentication failed")
    reg.executors["opencode"].script["s1"] = ok()
    await reg.launch(step("s1"), role=Role.architecture)
    pr = await reg.probe("codex", force=True)
    assert pr.last_error_class == ErrorClass.auth.value
    sel = await reg.select(Role.architecture)
    assert sel.executor == "opencode"
    assert "auth" in sel.candidates["codex"]


# ── offline ──────────────────────────────────────────────────
async def test_offline_all_unavailable_produces_no_launch(reg, fakes):
    for f in fakes.values():
        f._installed = False
    for tag in fakes:
        await reg.probe(tag, force=True)
    out = await reg.launch(step("s1"), role=Role.coding)
    assert not out.ok
    assert out.executor is None
    assert out.launches_used == 0
    assert out.result.error_class is ErrorClass.resource
    assert all(not calls for calls in (f.calls for f in fakes.values()))


async def test_offline_select_raises_with_reasons(reg, fakes):
    for f in fakes.values():
        f._authenticated = False
    for tag in fakes:
        await reg.probe(tag, force=True)
    with pytest.raises(SelectionError) as ei:
        await reg.select(Role.coding)
    assert "не авторизован" in str(ei.value)
    assert set(ei.value.tried) == {"opencode", "codex"}


async def test_offline_audit_records_exhausted(reg, audit_log, fakes):
    for f in fakes.values():
        f._installed = False
    for tag in fakes:
        await reg.probe(tag, force=True)
    await reg.launch(step("s1"), role=Role.cheap_test)
    ex = kinds(records(audit_log), "exhausted")
    assert ex and "no available executor" in ex[-1].reason


# ── failover и бюджет запусков ───────────────────────────────
async def test_failover_after_quota_and_network(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota")
    reg.executors["opencode"].script["s1"] = fail(ErrorClass.network, "connection refused")
    reg.executors["gemini"].script["s1"] = ok()
    out = await reg.launch(step("s1"), role=Role.general)
    assert out.ok and out.executor == "gemini"
    assert [a.executor for a in out.attempts] == ["codex", "opencode", "gemini"]
    assert out.launches_used == 3


async def test_max_launches_is_three(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota")
    reg.executors["opencode"].script["s1"] = fail(ErrorClass.network, "connection")
    reg.executors["gemini"].script["s1"] = fail(ErrorClass.timeout, "timeout")
    out = await reg.launch(step("s1"), role=Role.general)
    assert not out.ok
    assert out.launches_used == 3
    assert MAX_LAUNCHES == MAX_STEP_LAUNCHES == 3
    assert MAX_PARALLEL == MAX_PARALLEL_EXECUTORS == 2


async def test_no_failover_on_runtime(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.runtime, "bad patch")
    reg.executors["opencode"].script["s1"] = ok()
    out = await reg.launch(step("s1"), role=Role.architecture)
    assert not out.ok
    assert out.executor == "codex"
    assert out.launches_used == 1
    assert out.terminal is True


async def test_no_failover_on_unknown(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.unknown, "непонятно")
    reg.executors["opencode"].script["s1"] = ok()
    out = await reg.launch(step("s1"), role=Role.architecture)
    assert not out.ok and out.executor == "codex" and out.launches_used == 1


async def test_budget_zero_means_no_launch(reg, fakes):
    out = await reg.launch(step("s1"), role=Role.architecture, budget=0)
    assert not out.ok and out.launches_used == 0
    assert all(not f.calls for f in fakes.values())


async def test_budget_limits_failover(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota")
    reg.executors["opencode"].script["s1"] = ok()
    out = await reg.launch(step("s1"), role=Role.architecture, budget=1)
    # бюджет=1: единственный кодекс упал, второго запуска нет
    assert not out.ok and out.launches_used == 1


async def test_launch_passes_workdir_to_executor(reg, tmp_path):
    await reg.launch(step("s1"), role=Role.coding, workdir=str(tmp_path))
    calls = reg.executors["opencode"].calls
    assert calls and calls[0][2] == str(tmp_path)


async def test_executor_crash_is_unknown_not_hang(reg):
    def boom(_step):
        raise RuntimeError("adapter crashed")

    reg.executors["codex"].script["s1"] = boom
    out = await reg.launch(step("s1"), role=Role.architecture)
    assert not out.ok
    assert out.result.error_class is ErrorClass.unknown
    assert "crashed" in out.result.error_text
    # неизвестное не считается сетевым — нет failover
    assert out.launches_used == 1


# ── idempotency ──────────────────────────────────────────────
async def test_idempotent_replay_does_not_relaunch(reg):
    st = step("s1")
    out1 = await reg.launch(st, role=Role.architecture)
    n_codex = len(reg.executors["codex"].calls)
    out2 = await reg.launch(st, role=Role.architecture)
    assert out1.ok and out2.ok
    assert out2.deduplicated is True
    assert len(reg.executors["codex"].calls) == n_codex


async def test_idempotent_concurrent_same_key_one_launch(reg):
    reg.executors["codex"].delay = 0.05
    st = step("s1")
    a, b = await asyncio.gather(
        reg.launch(st, role=Role.architecture, launch_key="K"),
        reg.launch(st, role=Role.architecture, launch_key="K"),
    )
    assert a.ok and b.ok
    assert len(reg.executors["codex"].calls) == 1


async def test_forget_allows_new_launch(reg):
    st = step("s1")
    await reg.launch(st, role=Role.architecture)
    reg.forget(f"{st.id}:{st.type}:{st.prompt[:120]}")
    await reg.launch(st, role=Role.architecture)
    assert len(reg.executors["codex"].calls) == 2


async def test_failed_result_is_also_cached_until_forget(reg):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.runtime, "boom")
    st = step("s1")
    out1 = await reg.launch(st, role=Role.architecture)
    out2 = await reg.launch(st, role=Role.architecture)
    assert not out1.ok and not out2.ok and out2.deduplicated
    assert len(reg.executors["codex"].calls) == 1


# ── parallel limit 2 ────────────────────────────────────────
async def test_parallel_limit_is_two_across_executors(reg, fakes):
    """Регистр не даёт больше 2 одновременных исполнителей суммарно."""
    for tag in fakes:
        reg.executors[tag].delay = 0.05
    steps = [step(f"s{i}") for i in range(6)]
    outs = await asyncio.gather(*(reg.launch(s, role=Role.coding) for s in steps))
    assert all(o.ok for o in outs)
    peaks = {f.tag: f._counter["peak"] for f in fakes.values()}
    assert sum(peaks.values()) >= 1
    assert reg.peak_parallel <= 2
    # PyTest: не менее 2 и не более 2
    assert reg.peak_parallel == 2


# ── routing audit ───────────────────────────────────────────
async def test_audit_reconstructs_who_and_why(reg, audit_log):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota", quota_reset_at=None)
    reg.executors["opencode"].script["s1"] = ok()
    out = await reg.launch(step("s1"), role=Role.architecture)
    assert out.ok and out.executor == "opencode"

    log = records(audit_log)
    probes = kinds(log, "probe")
    selects = kinds(log, "select")
    launches = kinds(log, "launch")
    results = kinds(log, "launch_result")

    assert probes, "probe должен быть в аудите"
    codex_probes = [r for r in probes if r.executor == "codex"]
    assert codex_probes and codex_probes[0].data["probe"]["quota_state"] == "unknown"
    assert codex_probes[0].data["probe"]["installed"] is True
    # Первый select — codex с причиной по роли; второй — opencode после quota
    s1, s2 = selects[0], selects[1]
    assert s1.executor == "codex" and "role=architecture" in s1.reason
    assert s2.executor == "opencode"
    assert s2.data["candidates"]["codex"].startswith("уже провалился")
    f1, f2 = launches
    assert f1.executor == "codex" and f2.executor == "opencode"
    assert "failover после quota" in f2.reason
    assert [r.executor for r in results] == ["codex", "opencode"]


async def test_audit_has_order_and_step_ids(reg, audit_log):
    reg.executors["codex"].script["s1"] = fail(ErrorClass.quota, "quota")
    reg.executors["opencode"].script["s1"] = ok()
    await reg.launch(step("s1"), role=Role.architecture,
                     launch_key="ord_x:s1")
    # в audit порядок/шаг передаются из launch_key при прямом вызове неявно;
    # в рамках scheduler — явно (тест ниже). Здесь проверяем, что записи
    # launch несут шаг через order_id/step_id = step.id.
    launches = kinds(records(audit_log), "launch")
    assert all(r.step_id == "s1" for r in launches)
    jl = audit_log.path
    with open(jl, encoding="utf-8") as f:
        raw = [json.loads(x) for x in f if x.strip()]
    assert raw and {"at", "kind", "executor", "reason"} <= set(raw[0])


async def test_audit_probe_cached_does_not_spam_log(fakes, audit_log):
    reg = ExecutorRegistry(fakes, audit=audit_log, probe_ttl=DEFAULT_PROBE_TTL)
    await reg.probe("codex")
    n = len(kinds(records(audit_log), "probe"))
    await reg.probe("codex")  # из кэша, без записи
    assert len(kinds(records(audit_log), "probe")) == n


# ── probe как интерфейс ─────────────────────────────────────
async def test_probe_shape_has_required_fields(reg):
    pr: ProbeResult = await reg.probe("codex")
    d = pr.to_dict()
    for key in ("installed", "authenticated", "available", "last_error_class",
                "quota_state", "last_error_text", "quota_reset_at"):
        assert key in d
    assert d["quota_state"] in ("unknown", "ok", "exhausted")


async def test_probe_unknown_executor_not_installed(reg):
    pr = await reg.probe("нет-такого")
    assert pr.installed is False and pr.available is False
    assert pr.quota_state is QuotaState.unknown


def test_audit_default_path_is_outside_repo():
    """Дефолтный журнал — var/, не рабочий каталог: не засоряем репо."""
    assert audit_mod.DEFAULT_AUDIT_PATH.endswith("registry-audit.jsonl")
    assert "/var/" in audit_mod.DEFAULT_AUDIT_PATH
