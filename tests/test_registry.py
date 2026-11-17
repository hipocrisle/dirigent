"""Тесты registry / capability routing на fake executor.

Покрытие по заданию:
* selection по ролям;
* quota: known_exhausted исключается, note_quota -> failover;
* offline: все offline -> status=offline, не выбираем;
* unknown quota != available;
* parallel limit 2;
* routing audit: кто/почему выбран.

Никаких реальных CLI и расходования квоты — только FakeProber/факты.
"""
from __future__ import annotations

import asyncio

import pytest

from src.registry import (
    Budget,
    ExecutorRegistry,
    FakeProber,
    QuotaState,
    Role,
    SelectionStatus,
    make_report,
)
from src.registry.policy import ROLE_PREFERENCE


def _reg(**reports) -> ExecutorRegistry:
    """reports: tag -> ProbeReport (или None -> пробер не зарегистрирован)."""
    probers = {}
    for tag, rep in reports.items():
        if rep is not None:
            probers[tag] = FakeProber(rep)
    return ExecutorRegistry(probers=probers)


# ── selection по ролям ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_architecture_prefers_codex():
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"),
               gemini=make_report("gemini"))
    await reg.probe()
    sel = await reg.select(Role.architecture)
    assert sel.status is SelectionStatus.selected
    assert sel.tag == "codex"


@pytest.mark.asyncio
async def test_review_prefers_codex():
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"),
               gemini=make_report("gemini"))
    await reg.probe()
    assert (await reg.select(Role.review)).tag == "codex"


@pytest.mark.asyncio
async def test_coding_prefers_opencode():
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"),
               gemini=make_report("gemini"))
    await reg.probe()
    assert (await reg.select(Role.coding)).tag == "opencode"


@pytest.mark.asyncio
async def test_simple_test_prefers_gemini_when_available():
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"),
               gemini=make_report("gemini"))
    await reg.probe()
    assert (await reg.select(Role.simple_test)).tag == "gemini"


@pytest.mark.asyncio
async def test_simple_test_falls_back_when_gemini_missing():
    # Gemini не установлен: простые задачи уходят на OpenCode (второй в порядке)
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"),
               gemini=make_report("gemini", installed=False))
    await reg.probe()
    assert (await reg.select(Role.simple_test)).tag == "opencode"


@pytest.mark.asyncio
async def test_architecture_does_not_use_gemini_as_coder():
    # только Gemini доступен: для он не заявляет architecture/code, кодогога
    # в роли architecture не подменяем -> статус none, не тихий выбор gemini
    reg = _reg(gemini=make_report("gemini"))
    await reg.probe()
    sel = await reg.select(Role.architecture, capabilities={"code", "architecture"})
    assert sel.status is SelectionStatus.none
    assert sel.tag is None


# ── quota ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_exhausted_skipped_and_failover():
    reg = _reg(codex=make_report("codex", quota=QuotaState.known_exhausted),
               opencode=make_report("opencode"))
    await reg.probe()
    sel = await reg.select(Role.architecture)
    # Кодекс с исчерпанной квотой пропущен -> OpenCode
    assert sel.status is SelectionStatus.selected
    assert sel.tag == "opencode"
    assert any(r["tag"] == "codex" for r in sel.rejected)


@pytest.mark.asyncio
async def test_note_quota_cools_down_and_failover():
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"))
    await reg.probe()
    assert (await reg.select(Role.coding, key="o/s1")).tag == "opencode"
    # фиксируем исчерпание квоты OpenCode из адаптера
    reg.note_quota("opencode", cooldown_seconds=60)
    sel = await reg.select(Role.coding, key="o/s1")
    # pin снят, OpenCode в карантине -> fallback на codex
    assert sel.tag == "codex"
    # проверяем причину
    assert any(r["tag"] == "opencode" and "карантин" in r["why"]
               for r in sel.rejected)


@pytest.mark.asyncio
async def test_all_quota_exhausted_means_none():
    reg = _reg(codex=make_report("codex", quota=QuotaState.known_exhausted),
               opencode=make_report("opencode", quota=QuotaState.known_exhausted),
               gemini=make_report("gemini", quota=QuotaState.known_exhausted))
    await reg.probe()
    sel = await reg.select(Role.coding)
    assert sel.status is SelectionStatus.none
    assert sel.tag is None


# ── offline ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_offline_returns_offline():
    reg = _reg(codex=make_report("codex", offline=True),
               opencode=make_report("opencode", offline=True),
               gemini=make_report("gemini", offline=True))
    await reg.probe()
    sel = await reg.select(Role.coding)
    assert sel.status is SelectionStatus.offline
    assert sel.tag is None


@pytest.mark.asyncio
async def test_partial_offline_uses_rest():
    reg = _reg(codex=make_report("codex", offline=True),
               opencode=make_report("opencode"))
    await reg.probe()
    sel = await reg.select(Role.architecture)
    assert sel.status is SelectionStatus.selected
    assert sel.tag == "opencode"


@pytest.mark.asyncio
async def test_offline_env_flag(monkeypatch):
    from src.registry import CodexProber
    monkeypatch.setenv("DIRIGENT_REGISTRY_OFFLINE", "1")
    prober = CodexProber(auth_path="/nonexistent", which=lambda b: "/usr/bin/codex")
    rep = await prober.probe()
    assert rep.offline is True
    assert rep.available is False


# ── unknown quota != available ────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_quota_not_available_when_installed_false():
    # квота unknown и бинарь не установлен -> available=False, usable=False
    rep = make_report("codex", installed=False, quota=QuotaState.unknown)
    assert rep.quota_state is QuotaState.unknown
    assert rep.available is False
    assert rep.usable is False

    reg = _reg(codex=rep, opencode=make_report("opencode"))
    await reg.probe()
    sel = await reg.select(Role.architecture)
    # несмотря на предпочтение Codex, он недоступен
    assert sel.tag == "opencode"


@pytest.mark.asyncio
async def test_unknown_quota_alone_is_not_availability():
    # только Codex в реестре, установлен но без авторизации:
    # квота unknown НЕ превращает его в доступного
    reg = _reg(codex=make_report("codex", authorized=False))
    await reg.probe()
    sel = await reg.select(Role.review)
    assert sel.status is SelectionStatus.none


# ── parallel limit 2 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_parallel_limit_two():
    reg = _reg(opencode=make_report("opencode"))
    assert reg.max_parallel == 2
    assert reg.acquire_slot() is True       # 1-й
    assert reg.acquire_slot() is True       # 2-й
    assert reg.acquire_slot() is False      # 3-й отклонён
    assert reg.inflight == 2
    reg.release_slot()
    assert reg.acquire_slot() is True       # после освобождения
    reg.release_slot()
    reg.release_slot()
    assert reg.inflight == 0


@pytest.mark.parametrize("role", list(Role))
def test_role_preference_order_has_all_executors(role):
    order = ROLE_PREFERENCE[role]
    assert set(order) == {"codex", "opencode", "gemini"}
    assert len(order) == 3


# ── max launches / idempotency ────────────────────────────────

@pytest.mark.asyncio
async def test_max_three_launches():
    reg = _reg(opencode=make_report("opencode"))
    await reg.probe()
    key = "o/s1"
    assert (await reg.select(Role.coding, budget=Budget(max_launches=3), key=key)).status \
        is SelectionStatus.selected
    # фиксируем фактические запуски
    for _ in range(3):
        assert reg.note_launch(key, "opencode") <= 3
    assert reg.can_launch(key) is False
    # 4-й запуск — отказ
    sel = await reg.select(Role.coding, budget=Budget(max_launches=3), key=key)
    assert sel.status is SelectionStatus.exhausted


@pytest.mark.asyncio
async def test_budget_clamped_to_max_launches():
    reg = _reg(opencode=make_report("opencode"))
    await reg.probe()
    key = "o/s2"
    sel = await reg.select(Role.coding, budget=Budget(max_launches=99), key=key)
    assert sel.status is SelectionStatus.selected
    reg.note_launch(key, "opencode")
    reg.note_launch(key, "opencode")
    reg.note_launch(key, "opencode")
    sel = await reg.select(Role.coding, budget=Budget(max_launches=99), key=key)
    assert sel.status is SelectionStatus.exhausted


@pytest.mark.asyncio
async def test_idempotent_same_key_same_tag():
    reg = _reg(codex=make_report("codex"), opencode=make_report("opencode"))
    await reg.probe()
    key = "o/s3"
    s1 = await reg.select(Role.architecture, key=key)
    s2 = await reg.select(Role.architecture, key=key)
    assert s1.tag == s2.tag == "codex"
    assert "idempotent" in s2.reason


# ── routing audit ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_records_who_and_why():
    reg = _reg(codex=make_report("codex", quota=QuotaState.known_exhausted),
               opencode=make_report("opencode"))
    await reg.probe()
    sel = await reg.select(Role.coding, key="o/audit")
    a = reg.last_audit(key="o/audit")
    assert a is not None
    d = a.as_dict()
    assert d["selection"]["tag"] == "opencode"
    assert "role=coding" in d["selection"]["reason"]
    # пробы вошли в аудит
    assert d["probes"]["codex"]["quota_state"] == "known_exhausted"
    assert d["probes"]["opencode"]["available"] is True


@pytest.mark.asyncio
async def test_audit_marks_rejected_candidates():
    reg = _reg(codex=make_report("codex", authorized=False),
               opencode=make_report("opencode"))
    await reg.probe()
    sel = await reg.select(Role.architecture)
    a = reg.last_audit()
    rejected = {r["tag"]: r["why"] for r in a.selection.rejected}
    assert "codex" in rejected
    assert "not_authorized" in rejected["codex"]


def test_audit_serializes_to_dict():
    from src.registry.core import RoutingAudit
    from src.registry.types import Selection, Role, SelectionStatus
    sel = Selection(status=SelectionStatus.selected, tag="opencode", role=Role.coding,
                    reason="test")
    audit = RoutingAudit(at=1.0, key="k", role="coding", selection=sel)
    d = audit.as_dict()
    assert d["selection"]["tag"] == "opencode"
    assert d["role"] == "coding"
