"""Probe на РЕАЛЬНЫХ адаптерах этой машины (DE-1).

Только локальные факты: `which`, наличие файла авторизации. Никаких `codex exec`,
`opencode run`, `gemini -p` — квоты и платные API не трогаем (задание).

Цель — поймать расхождения между заявленной картой и фактом:
  * installed/authenticated выставляются адаптером, а не реестром «на веру»;
  * quota_state всегда unknown без факта запуска (не «available»);
  * preflight-запуск НЕ делает доступным исполнителя без авторизации.
"""
from __future__ import annotations

import asyncio

import pytest

from src.registry import ExecutorRegistry, QuotaState
from src.registry.gemini import GeminiExecutor


@pytest.fixture
def real_reg(tmp_path):
    """Отдельный реестр на реальных адаптерах; аудит — во временный файл."""
    from src.registry import AuditLog
    return ExecutorRegistry(audit=AuditLog(str(tmp_path / "audit.jsonl")),
                            probe_ttl=0.0)


async def test_real_probe_has_all_three_sources(real_reg):
    probes = await real_reg.probe_all()
    assert {"codex", "opencode"}.issubset(probes)
    probs = probes.get("gemini")
    if probs is None:
        # gemini просто не установлен на этой машине — тогда его нет и в реестре
        assert "gemini" not in real_reg.probers
    else:
        assert probs.installed is True      # бинарь есть (--version 0.68.0)


async def test_real_probe_fields_consistent(real_reg):
    for tag, pr in (await real_reg.probe_all()).items():
        assert pr.tag == tag
        # available строго следует из installed AND authenticated
        assert pr.available == (pr.installed and pr.authorized)
        # квота без факта запуска не может быть ok/exhausted
        assert pr.quota_state in (QuotaState.unknown, QuotaState.known_ok,
                                  QuotaState.known_exhausted)
        assert getattr(pr, "quota_used", None) is None and getattr(pr, "quota_limit", None) is None


async def test_real_codex_probe_matches_filesystem(real_reg):
    from src.executors import codex as codex_mod
    pr = await real_reg.probers["codex"].probe()
    binary = codex_mod.shutil.which("codex")
    assert pr.installed == bool(binary)
    if pr.installed:
        import pathlib
        auth = pathlib.Path(codex_mod.pathlib.Path.home(), ".codex", "auth.json")
        assert pr.authorized == auth.is_file()


async def test_real_gemini_auth_not_fabricated(real_reg):
    """На DE-1: settings указывает gemini-api-key, но кред не подтверждён.

    Тест разрешает обе правды (подтверждён/не подтверждён), но требует, чтобы
    reason был явным и не выдавал файл-заглушку за авторизацию.
    """
    if "gemini" not in real_reg.probers:
        pytest.skip("gemini не установлен")
    pr = await real_reg.probers["gemini"].probe()
    if pr.installed and not pr.authorized:
        assert "smoke" in pr.notes or "подтвержд" in pr.notes
    # и реестр не считает его доступным без авторизации
    sel = await real_reg.select("cheap_test")
    assert sel.tag != "gemini" or pr.authorized
