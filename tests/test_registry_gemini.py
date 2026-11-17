"""Тесты адаптера Gemini: classify, auth_status (без сети), run на фейке.

Ни одного реального вызова `gemini -p` — авторизация не подтверждена, пробы
платных API запрещены. Фейковый бинарь — синтетический (не копия вывода CLI).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import stat

import pytest

from src.models import ErrorClass, StepSpec
from src.registry import gemini
from src.registry.gemini import GeminiExecutor, auth_status, classify

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(KOREN, "tests", "fakes", "gemini-fake.sh")


def _progon(coro):
    return asyncio.run(coro)


@pytest.fixture
def gemini_fake(monkeypatch):
    os.chmod(FAKE, os.stat(FAKE).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr(gemini.shutil, "which",
                        lambda b: FAKE if b == "gemini-fake" else None)
    # env фейка — настоящий os.environ: ScriptExecutor с env={} наследует
    # PATH/HOME, а режим передаём переменной окружения как у codex-fake.
    return lambda mode: monkeypatch.setenv("GEMINI_FAKE_MODE", mode)


def _shag(prompt: str = "тест", timeout: float = 10.0) -> StepSpec:
    return StepSpec(id="s1", type="gemini", prompt=prompt, timeout_seconds=timeout)


@pytest.mark.parametrize("stderr, rc, klass", [
    ("Error: quota exceeded for project", 1, ErrorClass.quota),
    ("ResourceExhausted: 429", 1, ErrorClass.quota),
    ("API key not valid. Please pass a valid API key.", 1, ErrorClass.auth),
    ("Network error: getaddrinfo ENOTFOUND", 1, ErrorClass.network),
    ("плохо и непонятно", 1, ErrorClass.unknown),
    ("", 0, ErrorClass.ok),
    ("", 124, ErrorClass.timeout),
])
def test_classify(stderr, rc, klass):
    assert classify(stderr, rc) is klass


def test_quota_checked_before_auth():
    """«quota exceeded ... api key» — это квота, а не auth."""
    assert classify("Quota exceeded: your api key project is over limit", 1) \
        is ErrorClass.quota


# ── auth_status: только локальные факты ─────────────────────
def test_auth_env_key(tmp_path):
    ok, why = auth_status(str(tmp_path), {"GEMINI_API_KEY": "xxx"})
    assert ok and "env" in why


def test_auth_env_empty_not_counted(tmp_path):
    ok, why = auth_status(str(tmp_path), {"GEMINI_API_KEY": "  ", "GOOGLE_API_KEY": ""})
    assert not ok and "settings.json" in why


def test_auth_no_settings(tmp_path):
    ok, _ = auth_status(str(tmp_path), {})
    assert ok is False


def test_auth_settings_without_selected_type(tmp_path):
    g = tmp_path / ".gemini"
    g.mkdir()
    (g / "settings.json").write_text(json.dumps({"security": {}}), encoding="utf-8")
    ok, why = auth_status(str(tmp_path), {})
    assert not ok and "selectedType" in why


def test_auth_settings_unconfirmed_creds_file_is_not_proof(tmp_path):
    """Наличие gemini-credentials.json НЕ считается доказательством (DE-1 факт)."""
    g = tmp_path / ".gemini"
    g.mkdir()
    (g / "settings.json").write_text(json.dumps(
        {"security": {"auth": {"selectedType": "gemini-api-key"}}}), encoding="utf-8")
    (g / "gemini-credentials.json").write_text("{}", encoding="utf-8")
    ok, why = auth_status(str(tmp_path), {})
    assert ok is False
    assert "smoke" in why


def test_auth_settings_inline_api_key(tmp_path):
    g = tmp_path / ".gemini"
    g.mkdir()
    (g / "settings.json").write_text(json.dumps(
        {"security": {"auth": {"selectedType": "gemini-api-key", "apiKey": "k"}}}),
        encoding="utf-8")
    ok, why = auth_status(str(tmp_path), {})
    assert ok and "inline" in why


def test_available_requires_binary_and_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(gemini.shutil, "which", lambda b: None)
    assert _progon(GeminiExecutor(home=str(tmp_path), env={}).available()) is False
    monkeypatch.setattr(gemini.shutil, "which", lambda b: "/usr/bin/gemini")
    assert _progon(GeminiExecutor(home=str(tmp_path), env={}).available()) is False
    assert _progon(GeminiExecutor(home=str(tmp_path),
                                  env={"GEMINI_API_KEY": "k"}).available()) is True


# ── run на фейке ─────────────────────────────────────────────
def test_run_success(gemini_fake):
    gemini_fake("success")
    res = _progon(GeminiExecutor(binary="gemini-fake").run(_shag()))
    assert res.ok and res.log_tail.startswith("Ответ")


def test_run_quota(gemini_fake):
    gemini_fake("quota")
    res = _progon(GeminiExecutor(binary="gemini-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.quota


def test_run_auth(gemini_fake):
    gemini_fake("auth")
    res = _progon(GeminiExecutor(binary="gemini-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.auth


def test_run_silent_success_is_runtime(gemini_fake):
    gemini_fake("silent")
    res = _progon(GeminiExecutor(binary="gemini-fake").run(_shag()))
    assert not res.ok and res.error_class is ErrorClass.runtime


def test_run_timeout(gemini_fake):
    gemini_fake("hang")
    res = _progon(GeminiExecutor(binary="gemini-fake").run(_shag(timeout=0.3)))
    assert not res.ok and res.error_class is ErrorClass.timeout


def test_run_prompt_single_arg_no_shell(gemini_fake, tmp_path):
    gemini_fake("success")
    zhurnal = tmp_path / "argv.txt"
    import os as _os
    _os.environ["GEMINI_FAKE_ARGV"] = str(zhurnal)
    try:
        prompt = "привет; rm -rf /tmp/нет --yolo"
        assert _progon(GeminiExecutor(binary="gemini-fake").run(
            _shag(prompt=prompt), workdir=str(tmp_path))).ok
        lines = zhurnal.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "-p" and lines[1] == prompt
    finally:
        _os.environ.pop("GEMINI_FAKE_ARGV", None)


def test_no_yolo_in_default_args():
    """Безопасный дефолт: headless-просмотр, не --yolo."""
    assert "--yolo" not in gemini.DEFAULT_ARGS
    assert "-p" in gemini.DEFAULT_ARGS
