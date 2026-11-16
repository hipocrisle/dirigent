"""Тесты адаптеров WP-2: классификация, время сброса, available, run на фейках.
Реальная квота Codex не расходуется: codex.py проверяется только фейком."""
from __future__ import annotations

import asyncio
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


def _progon(coro):
    return asyncio.run(coro)


def _shag(prompt: str = "тест", timeout: float = 20.0) -> StepSpec:
    return StepSpec(id="s1", type="codex", prompt=prompt, timeout_seconds=timeout)


@pytest.fixture
def codex_fake(monkeypatch):
    put = _fajk("codex-fake.sh")
    monkeypatch.setattr(codex.shutil, "which", lambda b: put if b == "codex-fake" else None)
    return lambda mode: monkeypatch.setenv("CODEX_FAKE_MODE", mode)


@pytest.fixture
def opencode_fake(monkeypatch):
    put = _fajk("opencode-fake.sh")
    monkeypatch.setattr(opencode.shutil, "which", lambda b: put if b == "opencode-fake" else None)
    return lambda mode: monkeypatch.setenv("OPENCODE_FAKE_MODE", mode)


@pytest.mark.parametrize("mode, oshibka, reset_est", [
    ("quota", ErrorClass.quota, True),
    ("auth", ErrorClass.auth, False),
    ("network", ErrorClass.network, False),
    ("runtime", ErrorClass.unknown, False),
])
def test_codex_classify_stderr(mode, oshibka, reset_est):
    sreda = {**os.environ, "CODEX_FAKE_MODE": mode}
    gotovo = subprocess.run([os.path.join(FAKES, "codex-fake.sh"), "exec", "тест"],
                            capture_output=True, text=True, env=sreda)
    klass, reset = codex.classify(gotovo.stderr, gotovo.returncode)
    assert klass is oshibka and (reset is not None) is reset_est


def test_codex_classify_timeout_po_kodu_124():
    assert codex.classify("", 124)[0] is ErrorClass.timeout


def test_codex_classify_quota_bez_vremeni_daet_none():
    assert codex.classify("Error: Rate limit exceeded", 1) == (ErrorClass.quota, None)


@pytest.mark.parametrize("tekst, chas, minut", [
    ("resets at 4:36 PM", 16, 36), ("resets 16:36", 16, 36), ("resets at 12:05 AM", 0, 5),
    ("resets at 9:07 am", 9, 7), ("resets at 14:05 UTC", 14, 5),
])
def test_parse_reset_time_ponimaet_5_formats(tekst, chas, minut):
    gotovo = codex.parse_reset_time("limit reached; " + tekst)
    assert isinstance(gotovo, float)
    tm = time.localtime(gotovo)
    assert (tm.tm_hour, tm.tm_min) == (chas, minut) and tm.tm_mday == time.localtime().tm_mday


@pytest.mark.parametrize("tekst, sekund", [
    ("resets in 2 hours", 7200), ("resets in 90 minutes", 5400), ("resets in 1.5 hours", 5400),
])
def test_parse_reset_time_otnositelnoe_vremya(tekst, sekund):
    teper = time.time()
    gotovo = codex.parse_reset_time(tekst, now=teper)
    assert gotovo is not None and abs(gotovo - teper - sekund) < 1


@pytest.mark.parametrize("tekst", ["", "soon", "resets at 25:99", "лимит исчерпан"])
def test_parse_reset_time_neugadannoe_daet_none(tekst):
    assert codex.parse_reset_time(tekst) is None


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
    assert _progon(codex.CodexExecutor(binary="codex-fake").run(_shag())).error_class is ErrorClass.auth


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


def test_codex_run_prompt_odin_argument_argv(codex_fake, tmp_path, monkeypatch):
    """Пробелы и `;` в prompt не исполняются: он один элемент argv, shell не используется."""
    zhurnal = tmp_path / "argv.txt"
    monkeypatch.setenv("CODEX_FAKE_ARGV", str(zhurnal))
    codex_fake("success")
    prompt = "скажи привет; rm -rf /tmp/никуда --approve-for-me"
    assert _progon(codex.CodexExecutor(binary="codex-fake").run(_shag(prompt=prompt), workdir=str(tmp_path))).ok
    assert zhurnal.read_text(encoding="utf-8").split("\n")[:4] == ["exec", "--approve-for-me", prompt, ""]


@pytest.mark.parametrize("mode, oshibka", [
    ("auth", ErrorClass.auth), ("runtime", ErrorClass.runtime), ("network", ErrorClass.network),
])
def test_opencode_classify_stderr(mode, oshibka):
    sreda = {**os.environ, "OPENCODE_FAKE_MODE": mode}
    gotovo = subprocess.run([os.path.join(FAKES, "opencode-fake.sh"), "run", "--", "тест"],
                            capture_output=True, text=True, env=sreda)
    assert opencode.classify(gotovo.stderr, gotovo.returncode) is oshibka


def test_opencode_classify_kod_0_eto_ok():
    assert opencode.classify("", 0) is ErrorClass.ok


@pytest.mark.parametrize("stdout, zhdem", [
    ('Ответ: ок\n{"usage": {"tokens_in": 12, "tokens_out": 3, "usd": 0.0012}}',
     {"tokens_in": 12.0, "tokens_out": 3.0, "usd": 0.0012}),
    ('{"message": {"usage": {"tokens_in": 9, "tokens_out": 1, "usd": 0.0004}}}',
     {"tokens_in": 9.0, "tokens_out": 1.0, "usd": 0.0004}),
    ('{"type": "step-finish", "part": {"cost": 0.0015, "tokens": '
     '{"input": 230, "output": 3, "cache": {"read": 6528}}}}',
     {"tokens_in": 230.0, "tokens_out": 3.0, "usd": 0.0015}),
    ("Ответ без цифр", {}),
    ('{"usage": {"tokens_in": "много", "usd": true}}', {}),
])
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
    assert res.ok and res.usage == {"tokens_in": 12.0, "tokens_out": 3.0, "usd": 0.0012}


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
