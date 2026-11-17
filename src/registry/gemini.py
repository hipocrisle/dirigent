"""Адаптер Gemini CLI (headless `gemini -p ... -o text`).

ПОЯСНЕНИЕ ПРЕДПОЧТЕНИЯ «для дешёвых тестов»: это НЕ утверждение о цене.
Codex у нас — подписка (квотный ресурс, тратим окно), OpenCode/DeepSeek —
prepaid по факту usage. Gemini использует уже существующий локальный
ключа/авторизацию CLI; если его нет — исполнитель просто недоступен.

Классификация ошибок — как у codex/opencode: quota/auth/network по явным
маркерам, иначе unknown. Никаких проб платных API, никакого `--yolo` по
умолчанию (Gemini пишет файлы — только в worktree шага).

Авторизация (tier-1, без сети) — ГЕНОМЕНАЛЬНО КОНСЕРВАТИВНО:
  * env GEMINI_API_KEY / GOOGLE_API_KEY непустой, ИЛИ
  * в ~/.gemini/settings.json `security.auth.selectedType` задан И
    `security.auth.apiKey` непустой.
  Факт существует кред-файл `~/.gemini/gemini-credentials.json` в
  окружении DE-1 (12 KB) не удалось однозначно привязать к формату CLI,
  поэтому это НЕ является доказательством авторизации: скажем честно
  authenticated=False и «не подтверждено», а не «наверное ок».
  Подтверждение — один пробный `gemini -p` с разрешения владельца.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil

from ..models import ErrorClass, StepResult, StepSpec

QUOTA_MARKERS = ("quota", "usage limit", "rate limit", "exceeded your current quota",
                 "resource_exhausted", "resourceexhausted")
AUTH_MARKERS = ("authentication", "unauthorized", "api key", "login required",
                "auth error", "not logged in")
NETWORK_MARKERS = ("connection", "econnreset", "timed out", "dns", "network error",
                   "getaddrinfo")
DEFAULT_ARGS = ["-p", "{prompt}", "-o", "text"]


def classify(stderr: str, rc: int) -> ErrorClass:
    nizhnij = (stderr or "").lower()
    if rc in (124, 137, 143):
        return ErrorClass.timeout
    # порядок важен: quota проверяем раньше auth — «quota exceeded» часто
    # содержит «api key» в теле сообщения
    if any(z in nizhnij for z in QUOTA_MARKERS):
        return ErrorClass.quota
    if any(z in nizhnij for z in AUTH_MARKERS):
        return ErrorClass.auth
    if any(z in nizhnij for z in NETWORK_MARKERS):
        return ErrorClass.network
    return ErrorClass.unknown if rc != 0 else ErrorClass.ok


def _text_from_output(stream: str) -> str:
    """Сырой stdout/stderr Gemini; в StepResult.log_tail кладём хвост.

    Не разбираем JSON-строки: Gemini `-o text` даёт текст. Если формат
    изменится — это не влияет на успех/провал (успех = rc==0 и непустой
    текст), т.к. контракт шага всё равно проверяется acceptance.
    """
    return (stream or "").strip()[-800:]


def _env_key_present(env) -> bool:
    return bool((env.get("GEMINI_API_KEY") or "").strip()
                or (env.get("GOOGLE_API_KEY") or "").strip())


def auth_status(home: str | None = None, env=None) -> tuple[bool, str]:
    """(authenticated, reason). Без сети, без чтения значений ключей."""
    env = os.environ if env is None else env
    if _env_key_present(env):
        return True, "env:GEMINI_API_KEY/GOOGLE_API_KEY"
    root = pathlib.Path(home or pathlib.Path.home()) / ".gemini"
    settings = root / "settings.json"
    if not settings.is_file():
        return False, "no ~/.gemini/settings.json"
    try:
        cfg = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "settings.json unreadable"
    auth = ((cfg.get("security") or {}).get("auth") or {})
    selected = auth.get("selectedType")
    if not selected:
        return False, "security.auth.selectedType missing"
    if (auth.get("apiKey") or "").strip():
        return True, f"settings:{selected} + inline apiKey"
    # Наличие файла возле settings НЕ принимаем за кред: формат не подтверждён.
    return False, (f"settings:{selected}, но подтверждённого локального креда "
                   "нет (нужен smoke `gemini -p` с разрешения)")


class GeminiExecutor:
    tag = "gemini"

    def __init__(self, binary: str = "gemini", home: str | None = None,
                 args: list[str] | None = None, env: dict | None = None):
        self.binary = binary
        self.home = home or str(pathlib.Path.home())
        self.args = list(args) if args is not None else list(DEFAULT_ARGS)
        self._env = env  # для тестов: не трогаем глобальный os.environ

    def _environ(self) -> dict:
        # `env` — ПОЛНАЯ среда процесса, если задана (так CLI наследует только
        # то, что ему явно передали). Неуказанные ключи — из os.environ, чтобы
        # PATH/home не терялись. В тестах сюда кладут режим/argv фейка.
        d = os.environ.copy()
        if self._env is not None:
            d.update(self._env)
        return d

    async def available(self) -> bool:
        if not shutil.which(self.binary):
            return False
        ok, _ = auth_status(self.home, self._environ())
        return ok

    async def status(self, run_id: str) -> StepResult | None:
        return None

    async def cancel(self, run_id: str) -> None:
        return None

    async def run(self, step: StepSpec, workdir: str | None = None) -> StepResult:
        argv = [shutil.which(self.binary) or self.binary]
        for a in self.args:
            argv.append(step.prompt if a == "{prompt}" else a)
        try:
            proces = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=self._environ(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text=f"gemini не запускается: {e}")
        try:
            vyhod, oshibki = await asyncio.wait_for(
                proces.communicate(), timeout=step.timeout_seconds)
        except asyncio.TimeoutError:
            import os as _os
            import signal as _signal
            try:
                _os.killpg(_os.getpgid(proces.pid), _signal.SIGKILL)
            except Exception:
                try:
                    proces.kill()
                except Exception:
                    pass
            await proces.wait()
            return StepResult(ok=False, error_class=ErrorClass.timeout,
                              error_text="gemini: превышен таймаут шага")
        rc = proces.returncode
        stderr = oshibki.decode("utf-8", "replace")
        stdout = vyhod.decode("utf-8", "replace")
        klass = classify(stderr, rc)
        tail = _text_from_output(stdout)
        if klass is ErrorClass.ok and not stdout.strip():
            return StepResult(ok=False, error_class=ErrorClass.runtime,
                              error_text="gemini: пустой вывод при коде 0 (молчаливый успех)")
        if klass is ErrorClass.ok:
            return StepResult(ok=True, log_tail=tail)
        return StepResult(ok=False, error_class=klass,
                          error_text=stderr.strip()[:2000], log_tail=tail)
