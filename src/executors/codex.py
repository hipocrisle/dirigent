"""Адаптер исполнителя Codex (headless `codex exec`).

Квота Codex: tier-1 (PLAN AD-4) — только разбор stderr и кода выхода.
Никакого прощупывания API и обхода лимитов: неизвестный формат -> unknown.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import shutil
import time

from ..models import ErrorClass, StepResult, StepSpec

QUOTA_MARKERS = ("usage limit", "rate limit")
AUTH_MARKERS = ("authentication", "unauthorized", "login required")
NETWORK_MARKERS = ("connection", "econnreset", "timed out", "dns")

_RESET_AT = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2}):(\d{2})\s*([ap]m)?", re.IGNORECASE
)
_RESET_IN_HOURS = re.compile(
    r"resets?\s+in\s+(\d+(?:[.,]\d+)?)\s*(hours?|hrs?|h|часов|часа|час)",
    re.IGNORECASE,
)
_RESET_IN_MINUTES = re.compile(
    r"resets?\s+in\s+(\d+(?:[.,]\d+)?)\s*(minutes?|mins?|m|минут)", re.IGNORECASE
)
_RESET_ISO = re.compile(r"resets\s+at\s+(\d{1,2}):(\d{2})\s*UTC", re.IGNORECASE)


def parse_reset_time(text: str, now: float | None = None) -> float | None:
    """Время сброса квоты из текста в unix-time сегодняшнего дня.

    Понимаем «resets at 4:36 PM», «resets at 16:36», «resets 16:36»,
    «resets in 2 hours», «resets in 90 minutes», «resets at 14:05 UTC».
    Непонятный формат -> None (лучше unknown, чем выдуманное время).
    """
    if not text:
        return None
    teper = time.time() if now is None else now
    m = _RESET_ISO.search(text)
    if m:
        return _segodnya(int(m.group(1)), int(m.group(2)), teper)
    m = _RESET_IN_HOURS.search(text)
    if m:
        return teper + float(m.group(1).replace(",", ".")) * 3600.0
    m = _RESET_IN_MINUTES.search(text)
    if m:
        return teper + float(m.group(1).replace(",", ".")) * 60.0
    m = _RESET_AT.search(text)
    if not m:
        return None
    chas, minut = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm:
        if chas == 12:
            chas = 0
        if ampm == "pm":
            chas += 12
    if chas > 23 or minut > 59:
        return None
    return _segodnya(chas, minut, teper)


def _segodnya(chas: int, minut: int, teper: float) -> float:
    """Unix-time указанного часа СЕГОДНЯ по местному времени."""
    return time.mktime(time.localtime(teper)[:3] + (chas, minut, 0, 0, 0, -1))


def classify(stderr: str, rc: int):
    """Класс ошибки и время сброса квоты. Возвращает (ErrorClass, float|None).

    Ничего не угадываем: нет признака -> unknown.
    """
    nizhnij = (stderr or "").lower()
    if rc == 124:
        return ErrorClass.timeout, None
    if any(z in nizhnij for z in QUOTA_MARKERS):
        return ErrorClass.quota, parse_reset_time(stderr)
    if any(z in nizhnij for z in AUTH_MARKERS):
        return ErrorClass.auth, None
    if any(z in nizhnij for z in NETWORK_MARKERS):
        return ErrorClass.network, None
    return ErrorClass.unknown, None


class CodexExecutor:
    """Codex CLI через `codex exec --full-auto <prompt>`.

    available() — осознанное ограничение tier-1: проверяем только наличие
    бинаря и ~/.codex/auth.json, без сети и без расхода квоты (PLAN AD-4).
    status()/cancel() — для MVP заглушки: внешний процесс живёт вместе с run,
    учёт run_id — отдельный пакет.
    """

    tag = "codex"

    def __init__(self, binary: str = "codex", home: str | None = None):
        self.binary = binary
        self.home = home or str(pathlib.Path.home())

    async def available(self) -> bool:
        if not shutil.which(self.binary):
            return False
        return pathlib.Path(self.home, ".codex", "auth.json").is_file()

    async def status(self, run_id: str) -> StepResult | None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        argv = [shutil.which(self.binary) or self.binary, "exec", "--full-auto", step.prompt]
        try:
            proces = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text=f"codex не запускается: {e}")
        try:
            vyhod, oshibki = await asyncio.wait_for(
                proces.communicate(), timeout=step.timeout_seconds
            )
        except asyncio.TimeoutError:
            proces.kill()
            await proces.wait()
            # закрыть трубы до выхода из цикла события: иначе asyncio
            # ругается «Event loop is closed» при сборке мусора
            proces._transport.close()
            return StepResult(ok=False, error_class=ErrorClass.timeout,
                              error_text="codex: превышен таймаут шага")
        rc = proces.returncode
        stderr = oshibki.decode("utf-8", "replace")
        stdout = vyhod.decode("utf-8", "replace")
        if rc == 0 and not stdout.strip():
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text="codex: пустой вывод при коде 0 (молчаливый успех)")
        if rc == 0:
            return StepResult(ok=True)
        klass, reset = classify(stderr, rc)
        return StepResult(ok=False, error_class=klass, error_text=stderr.strip()[:2000],
                          quota_reset_at=reset)
