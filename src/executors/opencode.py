"""Адаптер исполнителя OpenCode (`opencode run --model ... -- <prompt>`).

Формат CLI сверен с `opencode run --help`: текст задачи — позиционные
аргументы, служебные флаги — до `--`, чтобы `-` в тексте не стал флагом.
Квота-класс для DeepSeek не используем: API у нас prepaid (PLAN AD-4).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import shutil

from ..models import ErrorClass, StepResult, StepSpec

AUTH_MARKERS = ("authentication fails",)
NETWORK_MARKERS = ("connection", "econnreset", "timed out", "dns")
DEFAULT_MODEL = "deepseek/deepseek-chat"
CONFIG = "/root/.config/opencode/opencode.json"
_KLUCHI_USAGE = ("tokens_in", "tokens_out", "usd")


def classify(stderr: str, rc: int) -> ErrorClass:
    """Класс ошибки по stderr и коду выхода; непонятное -> unknown."""
    nizhnij = (stderr or "").lower()
    if rc in (124, 137, 143):
        return ErrorClass.timeout
    if any(z in nizhnij for z in AUTH_MARKERS):
        return ErrorClass.auth
    if any(z in nizhnij for z in NETWORK_MARKERS):
        return ErrorClass.network
    if rc != 0:
        return ErrorClass.runtime
    return ErrorClass.ok


def usage_iz_stdout(stdout: str) -> dict[str, float]:
    """Последняя JSON-строка с usage -> токены и стоимость (только факт).

    Берём лишь известные числовые поля; ждём `usage` на верхнем уровне или
    в `message.usage`. Нет JSON -> {} (оценок не придумываем).
    """
    for stroka in reversed((stdout or "").splitlines()):
        stroka = stroka.strip()
        if not (stroka.startswith("{") and stroka.endswith("}")):
            continue
        try:
            dannye = json.loads(stroka)
        except json.JSONDecodeError:
            continue
        if not isinstance(dannye, dict):
            continue
        usage = dannye.get("usage")
        if usage is None and isinstance(dannye.get("message"), dict):
            usage = dannye["message"].get("usage")
        if not isinstance(usage, dict):
            continue
        fakt = {}
        for k in _KLUCHI_USAGE:
            v = usage.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fakt[k] = float(v)
        return fakt
    return {}


class OpencodeExecutor:
    """OpenCode CLI (DeepSeek). available() — бинарь + файл конфигурации."""

    tag = "opencode"

    def __init__(self, binary: str = "opencode", config: str = CONFIG):
        self.binary = binary
        self.config = config

    async def available(self) -> bool:
        return bool(shutil.which(self.binary)) and pathlib.Path(self.config).is_file()

    async def status(self, run_id: str) -> StepResult | None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        argv = [
            shutil.which(self.binary) or self.binary,
            "run", "--model", DEFAULT_MODEL, "--", step.prompt,
        ]
        try:
            proces = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text=f"opencode не запускается: {e}")
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
                              error_text="opencode: превышен таймаут шага")
        rc = proces.returncode
        stderr = oshibki.decode("utf-8", "replace")
        stdout = vyhod.decode("utf-8", "replace")
        usage = usage_iz_stdout(stdout)
        klass = classify(stderr, rc)
        if klass is ErrorClass.ok and not stdout.strip():
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text="opencode: пустой вывод при коде 0 (молчаливый успех)",
                              usage=usage)
        if klass is ErrorClass.ok:
            return StepResult(ok=True, usage=usage)
        return StepResult(ok=False, error_class=klass, usage=usage,
                          error_text=stderr.strip()[:2000])
