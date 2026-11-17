"""Доска уведомлений Kimi -> ChatGPT внутри MCP.

Канал без участия владельца: непрочитанные заметки прикрепляются к ответам
на частые вызовы (get_order/get_system_status/get_logs) и помечаются прочитанными.
"""
from __future__ import annotations

import sqlite3
import time

DB = "/matrix/dirigent/dirigent.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS notices ("
              " id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " ts REAL NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL,"
              " read INTEGER NOT NULL DEFAULT 0)")
    return c


def post(sender: str, text: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO notices(ts, sender, text) VALUES (?,?,?)",
                        (time.time(), sender[:40], text[:2000]))
        return cur.lastrowid


def pending() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id, ts, sender, text FROM notices"
                         " WHERE read=0 ORDER BY id").fetchall()
        return [{"id": r[0], "ts": r[1], "from": r[2], "text": r[3]}
                for r in rows]


def mark_read(ids: list[int]) -> None:
    if not ids:
        return
    with _conn() as c:
        c.execute(f"UPDATE notices SET read=1 WHERE id IN ({','.join('?' * len(ids))})",
                  ids)


def attach_notices(response: dict) -> dict:
    """Приклеить непрочитанные заметки к ответу MCP-инструмента."""
    notes = pending()
    if notes:
        response["notices_from_kimi"] = notes
        mark_read([n["id"] for n in notes])
    return response
