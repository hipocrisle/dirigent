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
    "pytest": None,        # требует workdir-репо, обрабатывается в run_acceptance
    "repo_changed": None,  # требует workdir-репо, обрабатывается в run_acceptance
}


def check_pytest(workdir: str) -> CheckResult:
    """Тесты проекта Python. Интерпретатор: .venv в worktree или в родителях
    (worktrees Дирижёра лежат в <repo>/worktrees/<x> — venv у родителя)."""
    import shutil
    if not (Path(workdir) / "pytest.ini").exists() and not (Path(workdir) / "setup.py").exists() \
            and not (Path(workdir) / "pyproject.toml").exists():
        return CheckResult(False, "pytest.ini/pyproject не найден — чекер pytest не для этого репо")
    py = None
    p = Path(workdir).resolve()
    for cand in [p, *p.parents[:4]]:
        v = cand / ".venv" / "bin" / "python"
        if v.exists():
            py = str(v)
            break
    if py is None:
        py = shutil.which("python3") or "python3"
    r = subprocess.run([py, "-m", "pytest", "-q"], cwd=workdir,
                       capture_output=True, text=True, timeout=1200)
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-10:])
    return CheckResult(p.returncode == 0, tail)


def check_repo_changed(workdir: str) -> CheckResult:
    """Coding-шаг обязан что-то изменить: «модель сказала готово» без результата — провал.

    Учитывает ОБА случая: незакоммиченные правки И коммиты агента поверх базы
    (иначе false negative: агент закоммитил -> дерево чистое -> ложный провал).
    """
    p = subprocess.run(["git", "-C", workdir, "status", "--porcelain"],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return CheckResult(False, "git status недоступен (не репо?)")
    dirty = [l for l in p.stdout.splitlines() if l.strip()]
    ahead = 0
    for base in ("dirigent/integrate", "dev", "main"):
        r = subprocess.run(["git", "-C", workdir, "rev-list", "--count",
                            f"{base}..HEAD"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            try:
                ahead = int(r.stdout.strip())
                break
            except ValueError:
                continue
    if dirty or ahead > 0:
        return CheckResult(True, f"изменений: {len(dirty)} dirty, {ahead} commits")
    return CheckResult(False, "рабочее дерево чистое и коммитов нет")


def check_files_exist(workdir: str, expect: list[str]) -> CheckResult:
    """Ожидаемые файлы созданы и непустые — против «успеха» без результата."""
    missing = []
    for rel in expect:
        p = Path(workdir) / rel
        if not p.exists() or p.stat().st_size == 0:
            missing.append(rel)
    return CheckResult(not missing,
                       f"нет файлов: {missing}" if missing else f"файлов ок: {len(expect)}")


def run_acceptance(step: StepSpec, workdir: str | None,
                   artifacts: dict[str, str]) -> CheckResult:
    """Все чекеры из step.acceptance.checks должны пройти."""
    checks: list = step.acceptance.get("checks", [])
    for name in checks:
        if name == "go_test":
            r = check_go_test(workdir or ".")
        elif name == "pytest":
            if not workdir:
                return CheckResult(False, "pytest: нет workdir")
            r = check_pytest(workdir)
        elif name == "repo_changed":
            if not workdir:
                return CheckResult(False, "repo_changed: нет workdir")
            r = check_repo_changed(workdir)
        elif name == "files_exist":
            expect = step.acceptance.get("expect_files", [])
            if not expect:
                return CheckResult(False, "files_exist: нет acceptance.expect_files")
            r = check_files_exist(workdir or ".", expect)
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
