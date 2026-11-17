"""E1: бюджет-контроллер. Факты, не оценки.

- Остаток DeepSeek: официальный GET /user/balance (проверено 17.11: работает,
  показывает $0.77 из $2). Кэш 60с, таблица budget_state.
- spent_total = topped_up - balance (честно при одном пополнении; при новом
  пополнении владелец говорит сумму — add_topup()).
- Политика: RESERVE_USD — ниже этого платные исполнители не запускаются;
  DAY_KNOWN_CAP — потолок известных списаний за сутки.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request

try:
    from .policy import DAY_BUDGET_USD
except ImportError:  # script-mode (systemd)
    from policy import DAY_BUDGET_USD

DB = "/matrix/dirigent/dirigent.db"
KEY_FILE = "/root/.config/deepseek.env"
RESERVE_USD = 0.20          # ниже — платные исполнители молчат
API = "https://api.deepseek.com/user/balance"
_topped_up_default = 2.0    # пополнение 16.11
_cache: tuple[float, float] | None = None


def _key() -> str:
    try:
        m = re.search(r"sk-[A-Za-z0-9]+", open(KEY_FILE).read())
        return m.group(0) if m else ""
    except Exception:
        return ""


def _topped_up() -> float:
    try:
        with sqlite3.connect(DB) as c:
            c.execute("CREATE TABLE IF NOT EXISTS budget_meta (k TEXT PRIMARY KEY, v REAL)")
            row = c.execute("SELECT v FROM budget_meta WHERE k='topped_up'").fetchone()
            if row:
                return float(row[0])
            c.execute("INSERT INTO budget_meta VALUES ('topped_up', ?)",
                      (_topped_up_default,))
            return _topped_up_default
    except Exception:
        return _topped_up_default


def add_topup(usd: float) -> None:
    new = _topped_up() + usd
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS budget_meta (k TEXT PRIMARY KEY, v REAL)")
        c.execute("INSERT OR REPLACE INTO budget_meta VALUES ('topped_up', ?)", (new,))


def balance() -> float | None:
    """Остаток USD (факт, None — не удалось узнать)."""
    global _cache
    if _cache and time.time() - _cache[0] < 60:
        return _cache[1]
    try:
        req = urllib.request.Request(API, headers={
            "Authorization": f"Bearer {_key()}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        for info in d.get("balance_infos", []):
            if info.get("currency") == "USD":
                bal = float(info["total_balance"])
                _cache = (time.time(), bal)
                with sqlite3.connect(DB) as c:
                    c.execute("CREATE TABLE IF NOT EXISTS budget_state"
                              " (ts REAL, balance_usd REAL)")
                    c.execute("INSERT INTO budget_state VALUES (?, ?)",
                              (time.time(), bal))
                return bal
    except Exception:
        pass
    return _cache[1] if _cache else None


def spent_total() -> float | None:
    if _cache is None and balance() is None:
        return None
    return round(_topped_up() - (_cache[1] if _cache else 0.0), 4)


def can_launch(tag: str) -> tuple[bool, str]:
    """(можно ли запускать, причина). Только для платных тегов; остальные — ок."""
    if tag not in ("opencode",):          # codex — подписка (квотный, не денежный)
        return True, "free-or-subscription"
    bal = balance()
    if bal is None:
        return False, "balance unknown — fail-closed (unknown != $0)"
    if bal < RESERVE_USD:
        return False, f"balance {bal:.2f} < reserve {RESERVE_USD}"
    return True, f"balance {bal:.2f}"


def report() -> dict:
    bal = balance()
    return {
        "deepseek_balance_usd": bal if bal is not None else "unknown",
        "deepseek_spent_total_usd": spent_total(),
        "topped_up_usd": _topped_up(),
        "daily_cap_usd": DAY_BUDGET_USD,
        "reserve_usd": RESERVE_USD,
        "codex_quota": "unknown",   # официального источника нет (AD-4)
        "note": "spent = topped_up - balance; при новом пополнении — add_topup()",
    }
