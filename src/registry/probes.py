"""Пробы исполнителей: факты installed/auth/available/offline/quota_state.

Только локальные и уже существующие данные, без внешних платных API:

* Codex — бинарь через shutil.which + ``~/.codex/auth.json`` (та же проверка,
  что в src/executors/codex.py::available). Квота НЕ прощупывается: tier-1
  (PLAN AD-4) — только из last_error/адаптера. По умолчанию unknown.
* OpenCode/DeepSeek — бинарь + ``~/.config/opencode/opencode.json`` (как в
  src/executors/opencode.py::available). API prepaid: квота known_ok только
  если это явно передано параметром, иначе unknown. Цифр расхода не выдумываем.
* Gemini — бинарь + локальный ``~/.gemini/gemini-credentials.json``. НЕ логинимся
  и НЕ устанавливаем: нет бинаря/креденшалов -> not_installed/not_authorized.

Offline — явный флаг (``offline=True`` или env ``DIRIGENT_REGISTRY_OFFLINE=1``).
Сеть не трогаем.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import time
from dataclasses import replace
from typing import Callable, Protocol

from .types import Availability, ProbeReport, QuotaState

OFFLINE_ENV = "DIRIGENT_REGISTRY_OFFLINE"


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HOME") or "/root").expanduser()


def _default_paths() -> dict[str, str]:
    home = _home()
    return {
        "codex": str(home / ".codex" / "auth.json"),
        "opencode": str(home / ".config" / "opencode" / "opencode.json"),
        "gemini": str(home / ".gemini" / "gemini-credentials.json"),
    }


class Prober(Protocol):
    tag: str

    async def probe(self) -> ProbeReport: ...


def _env_offline(override: bool | None) -> bool:
    if override is not None:
        return override
    return os.environ.get(OFFLINE_ENV, "0").lower() in ("1", "true", "yes", "on")


class _BinaryProber:
    """Общий скелет: бинарь + файл авторизации, квота по умолчанию unknown."""

    tag = ""
    binary = ""

    def __init__(self, auth_path: str | None = None, binary: str | None = None,
                 offline: bool | None = None,
                 which: Callable[[str], str | None] | None = None):
        self.auth_path = auth_path or _default_paths()[self.tag]
        self.binary = binary or self.tag
        self._offline_override = offline
        self._which = which or shutil.which

    async def probe(self) -> ProbeReport:
        offline = _env_offline(self._offline_override)
        found = self._which(self.binary)
        installed = bool(found)
        authorized = bool(self.auth_path) and pathlib.Path(self.auth_path).is_file()

        if offline:
            status = Availability.offline
        elif not installed:
            status = Availability.not_installed
        elif not authorized:
            status = Availability.not_authorized
        else:
            status = Availability.available

        detail = ""
        if not installed:
            detail = f"бинарь {self.binary!r} не найден"
        elif not authorized:
            detail = f"нет файла авторизации {self.auth_path}"
        elif offline:
            detail = "явный offline-флаг"

        return ProbeReport(
            tag=self.tag, installed=installed, authorized=authorized,
            available=status is Availability.available,
            quota_state=QuotaState.unknown, status=status, offline=offline,
            binary=found, detail=detail, checked_at=time.time(),
        )


class CodexProber(_BinaryProber):
    tag = "codex"


class OpencodeProber(_BinaryProber):
    tag = "opencode"

    def __init__(self, auth_path: str | None = None, binary: str | None = None,
                 offline: bool | None = None,
                 quota_state: QuotaState = QuotaState.unknown,
                 which: Callable[[str], str | None] | None = None):
        super().__init__(auth_path=auth_path, binary=binary, offline=offline,
                         which=which)
        self.quota_state = quota_state

    async def probe(self) -> ProbeReport:
        rep = await super().probe()
        # Prepaid API: known_ok только при явной передаче; иначе unknown.
        return replace(rep, quota_state=self.quota_state)


class GeminiProber(_BinaryProber):
    tag = "gemini"


class FakeProber:
    """Тестовый пробер: фиксированный отчёт, счётчик вызовов."""

    def __init__(self, report: ProbeReport):
        self.tag = report.tag
        self.report = report
        self.calls = 0

    async def probe(self) -> ProbeReport:
        self.calls += 1
        return self.report


def make_report(tag: str, *, installed: bool = True, authorized: bool = True,
                quota: QuotaState = QuotaState.unknown,
                offline: bool = False, last_error: str | None = None,
                last_error_class: str | None = None) -> ProbeReport:
    """Конструктор честного отчёта для тестов и интеграции (без догадок)."""
    if offline:
        status = Availability.offline
    elif not installed:
        status = Availability.not_installed
    elif not authorized:
        status = Availability.not_authorized
    else:
        status = Availability.available
    return ProbeReport(tag=tag, installed=installed, authorized=authorized,
                       available=status is Availability.available,
                       quota_state=quota, status=status, offline=offline,
                       last_error=last_error, last_error_class=last_error_class)
