"""Тесты адаптеров WP-2: классификация, время сброса, available, run на фейках.

Реальная квота Codex не расходуется: codex.py проверяется только фейком.
Реальный smoke opencode помечен `real`: .venv/bin/python -m pytest -m real.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time

import pytest

from src.executors import codex, opencode
from src.models import ErrorClass, StepSpec

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(KOREN, "tests", "fakes")


def _fajk(imya: str) -> str:
    put = os.path.join(FAKES, imya)
    os.chmod(put, os.stat(put).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return put


@pytest.fixture
def codex_fake(monkeypatch):
    path = _fajk("codex-fake.sh")
    monkeypatch.setattr(codex.shutil, "which",
                        lambda b: path if b == "codex-fake" else None)

    def podstavit(mode: str) -> None:
        monkeypatch.setenv("CODEX_FAKE_MODE", mode)

    return podstavit


@pytest.fixture
def opencode_fake(monkeypatch):
    path = _fajk("opencode-fake.sh")
    monkeypatch.setattr(opencode.shutil, "which",
                        lambda b: path if b == "opencode-fake" else None)
    pravila = os.path.join(FAKES, "opencode-fake.json")
    with open(pravila, "w", encoding="utf-8") as f:
        f.write("{}")
    return lambda mode: monkeypatch.setenv("OPENCODE_FAKE_MODE", mode)


def _shag(prompt: str = "тест", timeout: float = 20.0) -> StepSpec:
    return StepSpec(id="s1", type="codex", prompt=prompt, timeout_seconds=timeout)


# ───────────────────────── Codex: классификация ─────────────────────────


@pytest.mark.parametrize(
    "mode, oshibka, reset_est",
    [
        pytest.param("quota", ErrorClass.quota, True, id="quota-с-временем-сброса"),
        pytest.param("auth", ErrorClass.auth, False, id="auth-401"),
        pytest.param("network", ErrorClass.network, False, id="network-econnreset"),
        pytest.param("runtime", ErrorClass.unknown, False, id="runtime-не-угадываем"),
    ],
)
def test_codex_classify_stderr(codex_fake, mode, oshibka, reset_est):
    """Реальные строки фейка -> ожидаемый класс (неизвестное -> unknown)."""
    codex_fake(mode)
    gotovo = subprocess.run([os.path.join(FAKES, "codex-fake.sh"), mode],
                            capture_output=True, text=True, env={**os.environ})
    klass, reset = codex.classify(gotovo.stderr, gotovo.returncode)
    assert klass is oshibka
    assert (reset is not None) is reset_est


def test_codex_classify_timeout_po_kodu_124():
    assert codex.classify("", 124)[0] is ErrorClass.timeout


def test_codex_classify_quota_bez_vremeni_daet_none():
    """Слово лимита без времени сброса -> quota, reset_at=None (не выдумываем)."""
    klass, reset = codex.classify("Error: Rate limit exceeded", 1)
    assert klass is ErrorClass.quota
    assert reset is None


@pytest.mark.parametrize(
    "tekst, chas, minut",
    [
        pytest.param("resets at 4:36 PM", 16, 36, id="pm"),
        pytest.param("resets 16:36", 16, 36, id="24h"),
        pytest.param("resets at 12:05 AM", 0, 5, id="polnoch"),
        pytest.param("resets at 9:07 am", 9, 7, id="utro"),
        pytest.param("resets at 14:05 UTC", 14, 5, id="utc"),
    ],
)
def test_parse_reset_time_ponimaet_5_formats(tekst, chas, minut):
    """Время — сегодняшний день, локально (для UTC-варианта формат тот же)."""
    gotovo = codex.parse_reset_time("limit reached; " + tekst)
    assert isinstance(gotovo, float)
    tm = time.localtime(gotovo)
    assert (tm.tm_hour, tm.tm_min) == (chas, minut)
    assert tm.tm_mday == time.localtime().tm_mday


@pytest.mark.parametrize("tekst, sekund", [("resets in 2 hours", 7200), ("resets in 90 minutes", 5400),
                                          ("resets in 1.5 hours", 5400)])
def test_parse_reset_time_otnositelnoe_vremya(tekst, sekund):
    teper = time.time()
    gotovo = codex.parse_reset_time(tekst, now=teper)
    assert gotovo is not None and abs(gotovo - teper - sekund) < 1


@pytest.mark.parametrize("tekst", ["", "soon", "resets at 25:99", "ваш лимит исчерпан"])
def test_parse_reset_time_neugadannoe_daet_none(tekst):
    assert codex.parse_reset_time(tekst) is None


def test_parse_reset_time_pm_i_24h_sovpadayut():
    teper = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))
    a = codex.parse_reset_time("resets at 4:36 PM", now=teper)
    b = codex.parse_reset_time("resets 16:36", now=teper)
    assert a == b
    assert codex.parse_reset_time("resets at 12:05 am", now=teper) == \
        codex.parse_reset_time("resets 00:05", now=teper)


# ───────────────────────── Codex: available / run ─────────────────────────


def test_codex_available_bez_binarya(monkeypatch, tmp_path):
    monkeypatch.setattr(codex.shutil, "which", lambda b: None)
    assert _progon(codex.CodexExecutor(home=str(tmp_path)).available()) is False


def test_codex_available_binar_bez_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(codex.shutil, "which", lambda b: "/usr/bin/codex")
    assert _progon(codex.CodexExecutor(home=str(tmp_path)).available()) is False


def test_codex_available_binar_i_auth(monkeypatch, tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(codex.shutil, "which", lambda b: "/usr/bin/codex")
    assert _progon(codex.CodexExecutor(home=str(tmp_path)).available()) is True


def test_codex_run_success(codex_fake):
    codex_fake("success")
    res = _progon(codex.CodexExecutor(binary="codex-fake").run(_shag()))
    assert res.ok and res.error_class is ErrorClass.ok


def test_codex_run_quota_s_reset_time(codex_fake):
    codex_fake("quota")
    res = _progon(codex.CodexExecutor(binary="codex-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.quota
    assert isinstance(res.quota_reset_at, float)


def test_codex_run_auth(codex_fake):
    codex_fake("auth")
    res = _progon(codex.CodexExecutor(binary="codex-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.auth


def test_codex_run_molchashchij_uspeh_eto_runtime(codex_fake):
    codex_fake("silent")
    res = _progon(codex.CodexExecutor(binary="codex-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.runtime


def test_codex_run_hang_daet_timeout(codex_fake):
    codex_fake("hang")
    nachalo = time.monotonic()
    res = _progon(codex.CodexExecutor(binary="codex-fake").run(_shag(timeout=0.5)))
    assert not res.ok and res.error_class is ErrorClass.timeout
    assert time.monotonic() - nachalo < 10


def test_codex_workdir_peredan_processu(codex_fake, tmp_path):
    codex_fake("success")
    # фейк не пишет файлов — проверяем, что cwd принимается и не ломает запуск
    res = _progon(codex.CodexExecutor(binary="codex-fake").run(_shag(), workdir=str(tmp_path)))
    assert res.ok


# ───────────────────────── OpenCode ─────────────────────────


@pytest.mark.parametrize(
    "mode, oshibka",
    [
        pytest.param("auth", ErrorClass.auth, id="auth-authentication-fails"),
        pytest.param("runtime", ErrorClass.runtime, id="runtime-модель-недоступна"),
        pytest.param("network", ErrorClass.network, id="network-connection-refused"),
    ],
)
def test_opencode_classify_stderr(opencode_fake, mode, oshibka):
    opencode_fake(mode)
    gotovo = subprocess.run([os.path.join(FAKES, "opencode-fake.sh"), "run", "--", "тест"],
                            capture_output=True, text=True, env={**os.environ})
    assert opencode.classify(gotovo.stderr, gotovo.returncode) is oshibka


def test_opencode_classify_kod_0_eto_ok():
    assert opencode.classify("", 0) is ErrorClass.ok


@pytest.mark.parametrize(
    "stdout, zhdem",
    [
        ('Ответ: ок\n{"usage": {"tokens_in": 12, "tokens_out": 3, "usd": 0.0012}}',
         {"tokens_in": 12.0, "tokens_out": 3.0, "usd": 0.0012}),
        ('{"message": {"usage": {"tokens_in": 9, "tokens_out": 1, "usd": 0.0004}}}',
         {"tokens_in": 9.0, "tokens_out": 1.0, "usd": 0.0004}),
        ('Ответ без цифр', {}),
        ('{"usage": {"tokens_in": "много", "usd": true}}', {}),
    ],
)
def test_opencode_usage_iz_stdout(stdout, zhdem):
    assert opencode.usage_iz_stdout(stdout) == zhdem


def test_opencode_available_config_est():
    """На этой машине есть бинарь opencode и конфиг."""
    assert _progon(opencode.OpencodeExecutor().available()) is True


def test_opencode_available_bez_configa(monkeypatch, tmp_path):
    assert _progon(opencode.OpencodeExecutor(config=str(tmp_path / "нет.json")).available()) is False


def test_opencode_run_success_s_usage(opencode_fake):
    opencode_fake("success")
    res = _progon(opencode.OpencodeExecutor(binary="opencode-fake").run(_shag()))
    assert res.ok
    assert res.usage == {"tokens_in": 12.0, "tokens_out": 3.0, "usd": 0.0012}


def test_opencode_run_auth(opencode_fake):
    opencode_fake("auth")
    res = _progon(opencode.OpencodeExecutor(binary="opencode-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.auth


def test_opencode_run_runtime(opencode_fake):
    opencode_fake("runtime")
    res = _progon(opencode.OpencodeExecutor(binary="opencode-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.runtime


def test_opencode_run_hang_timeout(opencode_fake):
    opencode_fake("hang")
    res = _progon(opencode.OpencodeExecutor(binary="opencode-fake").run(_shag(timeout=0.5)))
    assert not res.ok and res.error_class is ErrorClass.timeout


# ───────────────────────── реальный smoke (вручную) ─────────────────────────


def test_opencode_real_smoke_odno_slovo():
    """Реальный вызов (маркер real): вручную .venv/bin/python -m pytest -m real."""
    res = _progon(opencode.OpencodeExecutor().run(
        _shag(prompt="Ответь одним словом: ок", timeout=180.0)))
    assert res.ok, res.error_text


# ───────────────────────── вспомогательное ─────────────────────────


def _progon(coro):
    """Запустить корутину без pytest-asyncio (синхронные тесты)."""
    import asyncio

    return asyncio.run(coro)
