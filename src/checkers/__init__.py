"""Acceptance checkers: machine-проверка результатов шага (AD-5).

«LLM сказал, что готово» — не доказательство. Чекер возвращает факт.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..models import StepSpec


class CheckResult(tuple):
    __slots__ = ()

    def __new__(cls, ok: bool, details: str = ""):
        return super().__new__(cls, (ok, details))

    @property
    def ok(self) -> bool: return self[0]
    @property
    def details(self) -> str: return self[1]


def check_go_test(workdir: str) -> CheckResult:
    if not (Path(workdir) / "go.mod").exists():
        return CheckResult(False, "go.mod не найден в workdir")
    p = subprocess.run(["go", "test", "./..."], cwd=workdir,
                       capture_output=True, text=True, timeout=900)
    tail = "\n".join((p.stdout + p.stderr).splitlines()[-15:])
    return CheckResult(p.returncode == 0, tail)


def check_diff_applies(workdir: str, diff_text: str) -> CheckResult:
    """Проверяем, что предложенный дифф реально применяется к чистому дереву."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["cp", "-a", workdir + "/.", tmp + "/"], timeout=120)
        p = subprocess.run(["git", "apply", "--check", "-"],
                           cwd=tmp, input=diff_text,
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            p = subprocess.run(["patch", "-p1", "--dry-run", "-d", tmp],
                               input=diff_text, capture_output=True, text=True,
                               timeout=120)
        return CheckResult(p.returncode == 0, (p.stderr or p.stdout)[-500:])


def check_artifact(path: str, acceptance: dict) -> CheckResult:
    """Артефакт существует и (если ffprobe есть и это mp4) метаданные совпадают."""
    p = Path(path)
    if not p.exists():
        return CheckResult(False, f"артефакт не найден: {path}")
    if p.stat().st_size == 0:
        return CheckResult(False, "артефакт пуст")
    want_dur = acceptance.get("duration_seconds")
    if want_dur and p.suffix == ".mp4" and shutil.which("ffprobe"):
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(p)],
            capture_output=True, text=True, timeout=120)
        try:
            dur = float(json.loads(r.stdout)["format"]["duration"])
            if abs(dur - float(want_dur)) > 1.0:
                return CheckResult(False, f"длительность {dur:.1f}s != {want_dur}")
        except Exception as e:
            return CheckResult(False, f"ffprobe parse: {e}")
    return CheckResult(True)


CHECKERS = {
    "go_test": check_go_test,
    "artifact": check_artifact,
}


def run_acceptance(step: StepSpec, workdir: str | None,
                   artifacts: dict[str, str]) -> CheckResult:
    """Все чекеры из step.acceptance.checks должны пройти."""
    checks: list = step.acceptance.get("checks", [])
    for name in checks:
        if name == "go_test":
            r = check_go_test(workdir or ".")
        elif name == "diff_apply":
            diff = step.inputs.get("diff", "")
            if not diff:
                return CheckResult(False, "diff_apply: нет inputs.diff")
            r = check_diff_applies(workdir or ".", diff)
        elif name == "artifact":
            outs = step.outputs or {}
            if not outs:
                return CheckResult(False, "artifact: нет outputs")
            first = artifacts.get(next(iter(outs))) or outs[next(iter(outs))]
            r = check_artifact(first, step.acceptance)
        else:
            return CheckResult(False, f"неизвестный чекер: {name}")
        if not r.ok:
            return CheckResult(False, f"{name}: {r.details}")
    return CheckResult(True)
