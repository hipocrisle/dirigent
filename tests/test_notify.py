"""P2: outbox уведомлений — dedup, режимы, отправка с фейковым sender'ом."""
import importlib
import os
import sqlite3

import pytest


@pytest.fixture
def notify(tmp_path, monkeypatch):
    monkeypatch.setattr(os.environ, "get", os.environ.get)
    m = importlib.import_module("src.notify")
    m = importlib.reload(m)
    monkeypatch.setattr(m, "JOURNAL_DB", str(tmp_path / "n.db"))
    return m


def test_enqueue_and_dedup(notify):
    assert notify.enqueue("order.done", "T", "body", "k1")
    assert not notify.enqueue("order.done", "T", "body", "k1")  # дубль
    assert notify.enqueue("order.error", "T", "b", "k2")        # другой ключ — ок
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        n = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert n == 2


def test_mode_filter(notify):
    notify.MODE = "quiet"
    assert not notify.enqueue("order.done", "T", "b", "k-done")   # фильтровано
    assert notify.enqueue("approval.requested", "T", "b", "k-apr")
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        rows = c.execute("SELECT kind FROM notifications").fetchall()
    assert [r[0] for r in rows] == ["approval.requested"]


def test_send_pending_with_fake_tg(notify, monkeypatch):
    sent = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({"ok": True, "result": {"message_id": 777}}).encode()

    def fake_urlopen(req, timeout=0):
        sent.append(req)
        return FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify, "_bot_token", lambda: "tok")
    notify.enqueue("order.done", "T", "b", "k1")
    assert notify.send_pending_once() == 1
    assert notify.send_pending_once() == 0  # второй проход — пусто
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        r = c.execute("SELECT status, tg_msg_id FROM notifications WHERE dedup_key='k1'").fetchone()
    assert r == ("sent", 777)


def test_send_failure_backoff(notify, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("tg down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    monkeypatch.setattr(notify, "_bot_token", lambda: "tok")
    notify.enqueue("order.error", "T", "b", "k1")
    assert notify.send_pending_once() == 0
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        r = c.execute("SELECT status, attempts, next_retry > 0 FROM notifications").fetchone()
    assert r[0] == "pending" and r[1] == 1 and r[2]
