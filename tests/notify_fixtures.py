"""Общий стенд для регрессий Telegram-уведомлений (тихий прод P4).

Реальная схема журнала Дирижёра (orders/steps/approvals/events) + изолированная
таблица outbox. Отправка TG всегда фейковая — тесты не выходят в сеть и не
трогают настоящий bot token.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time

import pytest

SCHEMA = """
CREATE TABLE orders (
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  budget_max_usd REAL, repo TEXT, state TEXT NOT NULL DEFAULT 'new',
  spec_json TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE steps (
  order_id TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  type TEXT NOT NULL, executor TEXT, spec_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
  backoff_until REAL, lease_owner TEXT, lease_until REAL, quota_reset_at REAL,
  result_json TEXT, error_class TEXT, error_text TEXT, updated REAL NOT NULL,
  PRIMARY KEY (order_id, id));
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, order_id TEXT,
  step_id TEXT, kind TEXT NOT NULL, data_json TEXT);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, order_id TEXT NOT NULL, step_id TEXT NOT NULL,
  question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  default_action TEXT NOT NULL DEFAULT 'wait', expires_at REAL,
  created REAL NOT NULL, resolved REAL);
"""


def make_notify(tmp_path, monkeypatch):
    m = importlib.import_module("src.notify")
    m = importlib.reload(m)
    db = str(tmp_path / "n.db")
    monkeypatch.setattr(m, "JOURNAL_DB", db)
    monkeypatch.setattr(m, "MODE", "quiet")   # прод-дефолт под тестом
    # Явное разрешение на доставку в не-прод окружении (см. notify.PRODUCTION_MODE):
    # без него sender_loop/авто-send молчат, чтобы тесты никогда не флудили.
    monkeypatch.setattr(m, "_allow_nonproduction_send", True, raising=False)
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA)
    m._test_db = db
    m._conn().close()   # создаём таблицу outbox заранее
    return m


class FakeTG:
    """Фейковый локальный bot API: пишет запросы, отвечает ok."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail = False
        self.reject_4xx = False

    def __call__(self, req, timeout=0):
        parsed = {k: v[0] for k, v in
                  __import__("urllib.parse").parse.parse_qs(
                      req.data.decode()).items()} if req.data else {}
        path = req.full_url.split("/bot", 1)[-1]        # "TOKEN/sendMessage"
        method = "/".join(path.split("/")[1:]) or path   # -> "sendMessage"
        self.calls.append({"method": method, **parsed})
        if self.reject_4xx:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
        if self.fail:
            raise OSError("tg down")
        msg_id = 1000 + len(self.calls)

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"ok": True,
                                   "result": {"message_id": msg_id}}).encode()

        return R()

    @property
    def messages(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "sendMessage"]


def install_fake_tg(notify, monkeypatch) -> FakeTG:
    fake = FakeTG()
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    monkeypatch.setattr(notify, "_bot_token", lambda: "tok")
    return fake


def add_order(m, order_id: str, *, repo: str | None = "/matrix/project",
              goal: str = "Реальная задача", state: str = "active",
              steps: list[dict] | None = None, constraints: dict | None = None,
              priority: int = 0) -> None:
    """steps: [{"id":..,"type":..,"state":..,"attempts":..,"max_attempts":..,
    "error_class":..,"danger":bool}]"""
    steps = steps or [{"id": "s1", "type": "opencode", "state": "running"}]
    spec = {"goal": goal, "priority": priority, "repo": repo,
            "constraints": constraints or {}, "steps": []}
    now = time.time()
    with sqlite3.connect(m._test_db) as c:
        c.execute("INSERT INTO orders(id,goal,priority,repo,state,spec_json,created,updated)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (order_id, goal, priority, repo, state, json.dumps(spec), now, now))
        for s in steps:
            sspec = {"id": s["id"], "type": s["type"], "danger": bool(s.get("danger"))}
            c.execute("INSERT INTO steps(order_id,id,state,type,spec_json,attempts,"
                      "max_attempts,error_class,error_text,updated)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (order_id, s["id"], s.get("state", "running"), s["type"],
                       json.dumps(sspec), s.get("attempts", 1),
                       s.get("max_attempts", 2), s.get("error_class"),
                       s.get("error_text"), now))


def add_approval(m, approval_id: str, order_id: str, step_id: str = "s1",
                 question: str = "Шаг s1 выполнен. Принять результат? ['patch.diff']",
                 status: str = "open", ttl: float | None = 3600.0,
                 default_action: str = "wait") -> None:
    now = time.time()
    with sqlite3.connect(m._test_db) as c:
        c.execute("INSERT INTO approvals(id,order_id,step_id,question,status,"
                  "default_action,expires_at,created) VALUES(?,?,?,?,?,?,?,?)",
                  (approval_id, order_id, step_id, question, status, default_action,
                   (now + ttl) if ttl else None, now))


def enqueue_approval(m, approval_id: str, order_id: str, step_id: str = "s1") -> bool:
    return m.enqueue("approval.requested", m.KIND_TITLES["approval.requested"],
                     "x", dedup_key=f"approval:{approval_id}", order_id=order_id,
                     step_id=step_id, approval_id=approval_id)


def rows(m, sql: str = "SELECT * FROM notifications", args=()) -> list[sqlite3.Row]:
    m._conn().close()  # ensure table exists even if nothing was enqueued yet
    with sqlite3.connect(m.JOURNAL_DB) as c:
        c.row_factory = sqlite3.Row
        return c.execute(sql, args).fetchall()
