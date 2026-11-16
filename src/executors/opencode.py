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


def _kill_tree(proces) -> None:
    """SIGKILL всей process-group (start_new_session): сироты невозможны."""
    import os
    import signal
    try:
        os.killpg(os.getpgid(proces.pid), signal.SIGKILL)
    except Exception:
        try:
            proces.kill()
        except Exception:
            pass


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


def _chislo(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def usage_iz_stdout(stdout: str) -> dict[str, float]:
    """Последняя JSON-строка с расходом -> токены и стоимость (только факт).

    Понимаем три формы (поля сверить с `--format json`, не выдумываем):
    1) {"usage": {"tokens_in", "tokens_out", "usd"}};
    2) {"message": {"usage": {...}}} — как в транскрипте сессии;
    3) строка события step-finish: {"type": "step-finish", "part":
       {"cost": 0.0012, "tokens": {"input": 230, "output": 3}}}
       (`input` без кэша: кэш из отдельного поля «cache» не прибавляем).
    Нет ни одного числа -> {}: оценок не придумываем.
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
        fakt: dict[str, float] = {}
        if isinstance(usage, dict):
            for k in _KLUCHI_USAGE:
                ch = _chislo(usage.get(k))
                if ch is not None:
                    fakt[k] = ch
        elif isinstance(dannye.get("part"), dict):
            chast = dannye["part"]
            tokeny = chast.get("tokens") if isinstance(chast.get("tokens"), dict) else {}
            pary = {
                "tokens_in": tokeny.get("input"),
                "tokens_out": tokeny.get("output"),
                "usd": chast.get("cost"),
            }
            for k, v in pary.items():
                ch = _chislo(v)
                if ch is not None:
                    fakt[k] = ch
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
                start_new_session=True,  # своя process-group: killpg убивает всё дерево
            )
        except OSError as e:
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text=f"opencode не запускается: {e}")
        try:
            vyhod, oshibki = await asyncio.wait_for(
                proces.communicate(), timeout=step.timeout_seconds
            )
        except asyncio.TimeoutError:
            _kill_tree(proces)
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
        tail = stdout.strip()[-800:]
        if klass is ErrorClass.ok and not stdout.strip():
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text="opencode: пустой вывод при коде 0 (молчаливый успех)",
                              usage=usage)
        if klass is ErrorClass.ok:
            return StepResult(ok=True, usage=usage, log_tail=tail)
        return StepResult(ok=False, error_class=klass, usage=usage,
                          error_text=stderr.strip()[:2000], log_tail=tail)
