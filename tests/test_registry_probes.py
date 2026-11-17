"""Тесты проб (probes) и моста failover — только локальные факты.

Внешние платные API не трогаем: which() и пути креденшалов подменяются на
временные. Проверяем installed/auth/available/offline/quota_state без сети.
"""
from __future__ import annotations

import pathlib

import pytest

from src.models import ErrorClass, StepResult
from src.registry import CodexProber, GeminiProber, OpencodeProber, QuotaState
from src.registry.types import Availability
from src.registry.bridge import failover_decision, record_failure


def _which(path: str | None):
    return lambda b: path


@pytest.mark.asyncio
async def test_probe_installed_and_authorized(tmp_path):
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    p = CodexProber(auth_path=str(cred), which=_which("/usr/bin/codex"), offline=False)
    rep = await p.probe()
    assert rep.installed and rep.authorized and rep.available
    assert rep.status is Availability.available
    assert rep.quota_state is QuotaState.unknown  # Codex tier-1: квоту не прощупываем


@pytest.mark.asyncio
async def test_probe_not_installed(tmp_path):
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    p = CodexProber(auth_path=str(cred), which=_which(None), offline=False)
    rep = await p.probe()
    assert rep.installed is False
    assert rep.available is False
    assert rep.status is Availability.not_installed


@pytest.mark.asyncio
async def test_probe_binary_but_no_auth(tmp_path):
    p = OpencodeProber(auth_path=str(tmp_path / "missing.json"),
                       which=_which("/usr/bin/opencode"), offline=False)
    rep = await p.probe()
    assert rep.installed is True and rep.authorized is False
    assert rep.status is Availability.not_authorized
    assert rep.available is False


@pytest.mark.asyncio
async def test_probe_offline_overrides(tmp_path):
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    p = GeminiProber(auth_path=str(cred), which=_which("/usr/bin/gemini"), offline=True)
    rep = await p.probe()
    assert rep.offline is True
    assert rep.status is Availability.offline
    assert rep.available is False


@pytest.mark.asyncio
async def test_opencode_quota_known_ok_only_when_explicit(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text("{}")
    p = OpencodeProber(auth_path=str(cfg), which=_which("/usr/bin/opencode"),
                       offline=False, quota_state=QuotaState.known_ok)
    rep = await p.probe()
    assert rep.quota_state is QuotaState.known_ok
    # по умолчанию unknown, а не «свободно»
    p2 = OpencodeProber(auth_path=str(cfg), which=_which("/usr/bin/opencode"),
                        offline=False)
    assert (await p2.probe()).quota_state is QuotaState.unknown


# ── bridge: квота/сеть от адаптера -> состояние реестра ───────────

@pytest.mark.asyncio
async def test_record_failure_quota_and_failover():
    from src.registry import ExecutorRegistry, FakeProber, make_report
    reg = ExecutorRegistry(probers={
        "opencode": FakeProber(make_report("opencode")),
        "codex": FakeProber(make_report("codex")),
    })
    await reg.probe()
    res = StepResult(ok=False, error_class=ErrorClass.quota,
                     quota_reset_at=None, error_text="rate limit")
    assert failover_decision(reg, "o/q", "opencode", res) is True
    # следующий выбор уходит на codex
    sel = await reg.select("coding", key="o/q")
    assert sel.tag == "codex"


@pytest.mark.asyncio
async def test_failover_not_on_runtime():
    from src.registry import ExecutorRegistry, FakeProber, make_report
    reg = ExecutorRegistry(probers={"opencode": FakeProber(make_report("opencode"))})
    await reg.probe()
    res = StepResult(ok=False, error_class=ErrorClass.runtime, error_text="boom")
    assert failover_decision(reg, "o/r", "opencode", res) is False


@pytest.mark.asyncio
async def test_failover_blocked_after_max_launches():
    from src.registry import ExecutorRegistry, FakeProber, make_report
    reg = ExecutorRegistry(probers={
        "opencode": FakeProber(make_report("opencode")),
        "codex": FakeProber(make_report("codex")),
    })
    await reg.probe()
    for _ in range(3):
        reg.note_launch("o/max", "opencode")
    res = StepResult(ok=False, error_class=ErrorClass.network)
    assert failover_decision(reg, "o/max", "opencode", res) is False


@pytest.mark.asyncio
async def test_record_failure_network_cools():
    from src.registry import ExecutorRegistry, FakeProber, make_report
    reg = ExecutorRegistry(probers={
        "opencode": FakeProber(make_report("opencode")),
        "codex": FakeProber(make_report("codex")),
    })
    await reg.probe()
    record_failure(reg, "opencode", StepResult(ok=False, error_class=ErrorClass.network))
    sel = await reg.select("coding")
    assert sel.tag == "codex"
    assert any(r["tag"] == "opencode" and "network" in r["why"] for r in sel.rejected)
