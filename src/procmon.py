"""Диагностика и безопасная остановка процессов исполнителей (инцидент ord_92c0f937c87f).

Поиск — по cwd: процессы исполнителей работают с cwd = worktree шага
(/matrix/dirigent/worktrees/<oid>-<sid>). Сканируем /proc, без изменений адаптеров.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

WORKTREES = Path("/matrix/dirigent/worktrees")


def worktree_path(oid: str, sid: str) -> str:
    from .workspace import safe_name
    return str(WORKTREES / f"{safe_name(oid)}-{safe_name(sid)}")


def find_pids(cwd_path: str) -> list[dict]:
    """Процессы, чей cwd == worktree (и их дети через pgid)."""
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cwd = os.readlink(p / "cwd")
        except Exception:
            continue
        if cwd != cwd_path:
            continue
        info = {"pid": int(p.name)}
        try:
            with open(p / "stat") as f:
                parts = f.read().rsplit(")", 1)[1].split()
            info["ppid"] = int(parts[1])
            info["stat"] = parts[0]
            info["etime_ticks"] = int(parts[19])
        except Exception:
            pass
        try:
            with open(p / "wchan") as f:
                info["wchan"] = f.read().strip() or "-"
        except Exception:
            info["wchan"] = "?"
        out.append(info)
    # расширяем детьми тех же групп
    if out:
        pgids = set()
        for i in out:
            try:
                pgids.add(os.getpgid(i["pid"]))
            except Exception:
                pass
        have = {i["pid"] for i in out}
        for pg in pgids:
            try:
                r = subprocess.run(["ps", "-eo", "pid,pgid,ppid,etime,pcpu,pmem,stat,args",
                                    "--no-headers"], capture_output=True, text=True, timeout=15)
                for line in r.stdout.splitlines():
                    cols = line.split(None, 7)
                    if len(cols) >= 8 and int(cols[1]) == pg and int(cols[0]) not in have:
                        out.append({"pid": int(cols[0]), "ppid": int(cols[2]),
                                    "etime": cols[3], "pcpu": cols[4], "pmem": cols[5],
                                    "stat": cols[6], "cmd": cols[7][:160], "child": True})
            except Exception:
                pass
    # ps-детали для корневых
    for i in out:
        if "cmd" not in i:
            try:
                r = subprocess.run(["ps", "-o", "pid,etime,pcpu,pmem,stat,args", "--no-headers",
                                    "-p", str(i["pid"])], capture_output=True, text=True, timeout=10)
                cols = r.stdout.split(None, 5)
                if len(cols) >= 6:
                    i.update({"etime": cols[1], "pcpu": cols[2], "pmem": cols[3],
                              "stat": cols[4], "cmd": cols[5][:160]})
            except Exception:
                pass
    return out


def diagnose(oid: str, sid: str, repo: str | None) -> dict:
    wt = worktree_path(oid, sid)
    procs = find_pids(wt)
    git = ""
    if repo and Path(wt).exists():
        try:
            r = subprocess.run(["git", "-C", wt, "status", "--short"],
                               capture_output=True, text=True, timeout=30)
            git = r.stdout.strip()
        except Exception as e:
            git = f"err: {e}"
    idle_net = [p for p in procs
                if p.get("wchan") == "ep_poll" and float(p.get("pcpu", "0").replace(",", ".")) < 1.0]
    return {
        "worktree": wt,
        "worktree_exists": Path(wt).exists(),
        "processes": procs,
        "git_status_short": git[:800],
        "hint": ("процессы в ep_poll с ~0% CPU подозрительно долго — вероятен сетевой сталл"
                 if idle_net and any("etime" in p and _long(p["etime"]) for p in idle_net)
                 else ""),
    }


def _long(etime: str) -> bool:
    # etime форматы: MM:SS или HH:MM:SS или D-HH:MM:SS
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-")
            days = int(d)
        parts = [int(x) for x in etime.split(":")]
        secs = parts[-1] + 60 * parts[-2] + (3600 * parts[-3] if len(parts) > 2 else 0)
        return days * 86400 + secs > 600
    except Exception:
        return False


def kill_processes(oid: str, sid: str) -> dict:
    """SIGTERM группам -> 10с ожидание -> SIGKILL. -> {killed:[pid], forced:[pid]}."""
    wt = worktree_path(oid, sid)
    procs = find_pids(wt)
    killed, forced = [], []
    pgids = set()
    for p in procs:
        try:
            pgids.add(os.getpgid(p["pid"]))
        except Exception:
            pass
    for pg in pgids:
        try:
            os.killpg(pg, signal.SIGTERM)
            killed.append(pg)
        except Exception:
            pass
    deadline = time.time() + 10
    while time.time() < deadline:
        left = [p for p in find_pids(wt)]
        if not left:
            break
        time.sleep(1)
    for pg in pgids:
        try:
            os.killpg(pg, 0)  # проверка живости
        except ProcessLookupError:
            continue
        except Exception:
            continue
        try:
            os.killpg(pg, signal.SIGKILL)
            forced.append(pg)
        except Exception:
            pass
    return {"killed_pg": killed, "forced_pg": forced,
            "remaining": [p["pid"] for p in find_pids(wt)]}
