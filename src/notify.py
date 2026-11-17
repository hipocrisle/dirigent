"""P2: Telegram-уведомления через transactional outbox (спека docs/P2-telegram-spec.md).

Дирижёр пишет намерение уведомления в SQLite (notifications, dedup_key UNIQUE) —
это переживает рестарт демона и недоступность Telegram. Sender-цикл доставляет
через локальный bot API (127.0.0.1:8081), backoff при сбоях, дубли отсекаются
уникальным ключом. Заказы НИКОГДА не блокируются недоступностью TG.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

JOURNAL_DB = "/matrix/dirigent/dirigent.db"
TG_API = "http://127.0.0.1:8081"
ADMIN_CHAT = 424106681  # ADMIN_ID из telegram-bot/bot.py

# Режимы: quiet — критичное+апрувы; normal — +done; verbose — +прочее (дефолт на тест)
MODES = {
    "quiet": {"approval.requested", "order.error"},
    "normal": {"approval.requested", "order.error", "order.done"},
    "verbose": {"approval.requested", "order.error", "order.done",
                "step.retry_refused", "order.paused", "order.resumed"},
}
MODE = os.environ.get("DIRIGENT_TG_MODE", "quiet")

KIND_TITLES = {
    "approval.requested": "🙅 Требуется решение",
    "order.done": "✅ Заказ завершён",
    "order.error": "❌ Заказ провалился",
    "step.retry_refused": "⛔ Попытки исчерпаны",
    "order.paused": "⏸ Заказ на паузе",
    "order.resumed": "▶️ Заказ продолжён",
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(JOURNAL_DB)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS notifications ("
              " id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " kind TEXT NOT NULL, order_id TEXT, step_id TEXT, approval_id TEXT,"
              " title TEXT NOT NULL, body TEXT NOT NULL,"
              " dedup_key TEXT UNIQUE NOT NULL,"
              " status TEXT NOT NULL DEFAULT 'pending',"
              " attempts INTEGER NOT NULL DEFAULT 0,"
              " next_retry REAL NOT NULL DEFAULT 0,"
              " tg_msg_id INTEGER,"
              " created REAL NOT NULL, sent_at REAL)")
    return c


HOURLY_CAP = 10  # анти-спам: не более N уведомлений в час, остальные отбрасываются


def _hour_count() -> int:
    try:
        with _conn() as c:
            return c.execute("SELECT COUNT(*) FROM notifications"
                             " WHERE created > ?", (time.time() - 3600,)).fetchone()[0]
    except Exception:
        return HOURLY_CAP  # при сомнениях — молчим


def enqueue(kind: str, title: str, body: str, dedup_key: str,
            order_id: str | None = None, step_id: str | None = None,
            approval_id: str | None = None) -> bool:
    """Идемпотентно. False — дубль, режим фильтрует или сработал анти-спам-кэп."""
    if kind not in MODES.get(MODE, MODES["verbose"]):
        return False
    if kind != "approval.requested" and _hour_count() >= HOURLY_CAP:
        return False
    try:
        with _conn() as c:
            c.execute("INSERT INTO notifications(kind, order_id, step_id, approval_id,"
                      " title, body, dedup_key, created) VALUES (?,?,?,?,?,?,?,?)",
                      (kind, order_id, step_id, approval_id, title, body[:900],
                       dedup_key, time.time()))
        return True
    except sqlite3.IntegrityError:
        return False  # дубль — ок
    except Exception:
        return False  # уведомления не должны ломать заказы


def _bot_token() -> str | None:
    try:
        import sys
        sys.path.insert(0, "/matrix/vault")
        from secrets_vault import vault
        return vault.get("bot.token")
    except Exception:
        return None


def _tg_send(token: str, text: str, keyboard: list | None = None) -> int | None:
    """-> message_id | None."""
    data = {"chat_id": ADMIN_CHAT, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    req = urllib.request.Request(
        f"{TG_API}/bot{token}/sendMessage",
        data=urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read().decode())
        if out.get("ok"):
            return out["result"]["message_id"]
    return None


def approval_keyboard(approval_id: str) -> list:
    return [[
        {"text": "✅ Разрешить", "callback_data": f"dgt|apr|{approval_id}|ok"},
        {"text": "❌ Отклонить", "callback_data": f"dgt|apr|{approval_id}|no"},
    ]]


def send_pending_once() -> int:
    """Один проход отправки. -> число отправленных. Backoff: 5с/30с/300с."""
    token = _bot_token()
    if not token:
        return 0
    sent = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM notifications WHERE status='pending' AND next_retry<=?"
            " ORDER BY id LIMIT 20", (time.time(),)).fetchall()
        for r in rows:
            msg_id = None
            try:
                kb = approval_keyboard(r["approval_id"]) if r["kind"] == "approval.requested" else None
                msg_id = _tg_send(token, f"<b>{r['title']}</b>\n\n{r['body']}", kb)
            except Exception:
                msg_id = None
            if msg_id:
                c.execute("UPDATE notifications SET status='sent', tg_msg_id=?, sent_at=?,"
                          " attempts=attempts+1 WHERE id=?", (msg_id, time.time(), r["id"]))
                sent += 1
            else:
                backoff = [5, 30, 300][min(r["attempts"], 2)]
                c.execute("UPDATE notifications SET attempts=attempts+1, next_retry=?"
                          " WHERE id=?", (time.time() + backoff, r["id"]))
        c.commit()
    return sent


async def sender_loop(interval: float = 3.0, stop=None) -> None:
    import asyncio
    while stop is None or not stop.is_set():
        try:
            await asyncio.to_thread(send_pending_once)
        except Exception:
            pass
        await asyncio.sleep(interval)
