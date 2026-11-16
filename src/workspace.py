"""Git worktree-изоляция coding-шагов (AD-11).

Каждый кодовый шаг работает в своём worktree и ветке dirigent/<order>-<step>.
После успешной приёмки ветка сливается в НЕ основную ветку — dirigent/integrate
(создаётся от текущего HEAD, если нет). Конфликт слияния = worktree сохраняется,
в отчёт уходит имя ветки для ручного разбора. Основная ветка репо не трогается,
force push и destructive-команды запрещены (спека §8.4).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

BASE = Path("/matrix/dirigent/worktrees")
INTEGRATE_BRANCH = "dirigent/integrate"


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:60]


def _git(repo: str, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True, timeout=timeout, check=check)


def ensure_worktree(repo: str, oid: str, sid: str) -> tuple[str, str]:
    """(path, branch). Создаёт worktree+ветку, если ещё нет."""
    name = f"{safe_name(oid)}-{safe_name(sid)}"
    branch = f"dirigent/{name}"
    path = BASE / name
    BASE.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return str(path), branch
    _git(repo, "worktree", "add", "-b", branch, str(path))
    return str(path), branch


def commit_all(workdir: str, message: str) -> bool:
    """Закоммитить все изменения в worktree; False — нечего коммитить."""
    if not _git(workdir, "status", "--porcelain").stdout.strip():
        return False
    _git(workdir, "add", "-A")
    _git(workdir, "-c", "user.email=dirigent@local", "-c",
         "user.name=dirigent", "commit", "-qm", message[:200])
    return True


def merge_to_integrate(repo: str, branch: str) -> tuple[bool, str]:
    """Слить ветку шага в dirigent/integrate. (ok, detail)."""
    cur = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    try:
        exists = _git(repo, "rev-parse", "--verify", INTEGRATE_BRANCH, check=False).returncode == 0
        if not exists:
            _git(repo, "branch", INTEGRATE_BRANCH, "HEAD")
        _git(repo, "checkout", INTEGRATE_BRANCH)
        r = _git(repo, "merge", "--no-ff", "-m", f"dirigent: merge {branch}", branch, check=False)
        if r.returncode != 0:
            _git(repo, "merge", "--abort", check=False)
            return False, f"conflict: {r.stderr[-300:]}"
        return True, f"merged into {INTEGRATE_BRANCH}"
    finally:
        if cur and cur != "HEAD":
            _git(repo, "checkout", cur, check=False)


def cleanup_worktree(repo: str, path: str, branch: str,
                     delete_branch: bool = True) -> None:
    _git(repo, "worktree", "remove", "--force", path, check=False)
    if delete_branch:
        _git(repo, "branch", "-D", branch, check=False)
