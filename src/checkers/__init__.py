"""Acceptance checkers: machine-проверка результатов шага (AD-5).

«LLM сказал, что готово» — не доказательство. Чекер возвращает факт.
"""
from __future__ import annotations

import json
import os
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
    root = Path(workdir).resolve()
    for cand in [root, *root.parents[:4]]:
        v = cand / ".venv" / "bin" / "python"
        if v.exists():
            py = str(v)
            break
    if py is None:
        py = shutil.which("python3") or "python3"
    r = subprocess.run([py, "-m", "pytest", "-q"], cwd=workdir,
                       capture_output=True, text=True, timeout=1200)
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-10:])
    return CheckResult(r.returncode == 0, tail)


def _changed_paths(workdir: str, ref: str) -> list[str]:
    """Файлы, реально отличающиеся от ref (закоммиченные + рабочее дерево).

    Пустые коммиты (--allow-empty) изменением не считаются: сравнивается
    содержимое деревьев, а не число коммитов.
    """
    p = _git_out(workdir, "diff", "--name-only", ref, "--")
    return [l for l in p.stdout.splitlines() if l.strip()]


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git hash-object -t tree /dev/null


def _git_out(workdir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", workdir, *args],
                          capture_output=True, text=True, timeout=60)


def _repo_base_refs(workdir: str) -> tuple[list[str], str]:
    """Референсы, относительно которых считается «база» работы шага.

    Приоритет:
    1. Маркер `.git/dirigent_base` — SHA вершины репозитория в момент
       создания worktree шага; его пишет workspace.ensure_worktree, чтобы
       база не «уезжала» при последующих коммитах в основную ветку.
    2. worktree Дирижёра (dirigent/<oid>-<sid>): merge-base с основной
       веткой репо и с dirigent/integrate.
    3. Обычный репозиторий: точка ветвления текущей ветки от другой ветки
       (merge-base), если такая есть.

    База по SHA, предшествующая HEAD; без внешней базы возвращается [] и
    работа считается «весь HEAD относительно пустого дерева».
    """
    base_refs: list[str] = []
    branch = _git_out(workdir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    top = _git_out(workdir, "rev-parse", "--show-toplevel").stdout.strip()
    if top:
        marker = _git_out(workdir, "rev-parse", "--git-dir").stdout.strip()
        mpath = Path(top if os.path.isabs(marker) else os.path.join(top, marker)) / "dirigent_base"
        try:
            sha = mpath.read_text().strip()
        except OSError:
            sha = ""
        # маркер валиден, только если он предок HEAD: иначе он от другой работы
        if sha and _git_out(workdir, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0:
            base_refs.append(sha)
            return base_refs, branch
    if not branch or branch == "HEAD":
        return base_refs, branch
    if branch.startswith("dirigent/"):
        cands = ["dirigent/integrate"]
        cands += [c for c in os.environ.get("DIRIGENT_BASE_BRANCHES", "").split() if c] \
            or ["main", "master"]
        for cand in cands:
            if cand == branch:
                continue
            if _git_out(workdir, "rev-parse", "--verify", "-q", cand).returncode != 0:
                continue
            mb = _git_out(workdir, "merge-base", branch, cand)
            if mb.returncode == 0 and mb.stdout.strip():
                sha = mb.stdout.strip()
                if sha not in base_refs:
                    base_refs.append(sha)
        return base_refs, branch
    # обычный репозиторий: база — точка ветвления от другой ветки (если есть)
    others = [b.lstrip("* ").strip() for b in
              _git_out(workdir, "branch", "--format=%(refname:short)").stdout.splitlines()]
    for other in others:
        other = other.strip()
        if not other or other == branch:
            continue
        mb = _git_out(workdir, "merge-base", branch, other)
        if mb.returncode == 0 and mb.stdout.strip():
            sha = mb.stdout.strip()
            if sha not in base_refs:
                base_refs.append(sha)
    return base_refs, branch



def check_repo_changed(workdir: str) -> CheckResult:
    """Coding-шаг обязан что-то изменить относительно базы.

    Изменения определяются относительно базы шага — маркера `.git/dirigent_base`
    (SHA в момент создания worktree) или merge-base с основной веткой /
    dirigent/integrate — и включают и закоммиченные агентом правки, и
    незакоммиченные. `git status --porcelain` в одиночку не критерий: агент
    вправе сам закоммитить результат. Предсуществующие коммиты (в базе, в
    основной ветке, чужие пустые коммиты) false positive не дают.
    """
    st = _git_out(workdir, "status", "--porcelain")
    if st.returncode != 0:
        return CheckResult(False, "git status недоступен (не репо?)")
    uncommitted = [l for l in st.stdout.splitlines() if l.strip()]

    head = _git_out(workdir, "rev-parse", "--verify", "-q", "HEAD")
    if head.returncode != 0:
        # репо без коммитов: любые файлы (staged/untracked) — уже изменение
        return CheckResult(bool(uncommitted),
                           f"изменений: {len(uncommitted)}" if uncommitted
                           else "рабочее дерево чистое")

    base_refs, _branch = _repo_base_refs(workdir)
    if not base_refs:
        # внешней базы нет: весь HEAD относительно пустого дерева
        base_refs = [_EMPTY_TREE]
    bases = [("<root>" if r == _EMPTY_TREE else r[:8]) for r in base_refs]

    committed = _commits_since(workdir, base_refs[0])
    changed = _changed_paths(workdir, base_refs[0])

    total = len(changed)
    if total:
        parts = []
        if committed:
            parts.append(f"коммитов: {len(committed)}")
        parts.append(f"файлов: {total}")
        if uncommitted:
            parts.append(f"незакоммиченных записей: {len(uncommitted)}")
        return CheckResult(True, f"изменений: {total} ({', '.join(parts)}; база {','.join(bases)})")
    if uncommitted:
        # только новые неотслеживаемые файлы: diff против базы их не видит
        untracked = [l for l in uncommitted if l.startswith("??")]
        if untracked:
            return CheckResult(True, f"изменений: {len(untracked)} "
                                     f"(новых файлов: {len(untracked)}; база {','.join(bases)})")
    if committed:
        # коммиты есть, но итоговое содержимое совпадает с базой
        # (пустые коммиты / правка с последующим откатом)
        return CheckResult(False, f"изменений в дереве нет относительно базы "
                                  f"{','.join(bases)} (коммитов без изменения итога: "
                                  f"{len(committed)})")
    return CheckResult(False, f"рабочее дерево чистое и нет коммитов относительно базы "
                              f"{','.join(bases)}")


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
