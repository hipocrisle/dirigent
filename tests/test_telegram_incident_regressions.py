"""Регрессии к ИНЦИДЕНТУ 17.11: «тихий прод» Telegram из живого демона.

Все 2055 сообщений инцидента ушли через `sender_loop` ДЕМОНА (порт 8081), а не
из тестов: в тот момент демон работал со старым кодом, пока в том же рабочем
дереве прогонялись тесты, создававшие pending-уведомления. Здесь проверяется
последний рубеж:

  * вне прод-режима (подменённый журнал/режим) sender молчит;
  * в проде работают dedup по event_id, order+transition и rate-limit, но
    критические апрувы не теряются;
  * тестовые/служебные заказы молчат на уровне enqueue;
  * API: однократное approve/reject, просрочка, чужой заказ;
  * кнопки: callback_data ≈ approval_id из карточки outbox, чужие/мусорные — отказ.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import sqlite3
import time

import pytest

from tests.notify_fixtures import add_approval, add_order, rows
from tests.test_actions_api import client  # noqa: F401  — fixture API-сервера


# ── sender: предохранитель вне прода ─────────────────────────
def test_nonproduction_sender_never_sends(notify, tg, monkeypatch):
    """Даже если в не-прод окружении что-то попало в outbox — не шлём."""
    add_order(notify, "ord_real", steps=[{"id": "s1", "type": "opencode"}])
    assert notify.enqueue("order.error", "T", "b", "e", order_id="ord_real")
    monkeypatch.setattr(notify, "_allow_nonproduction_send", False)
    monkeypatch.setattr(notify, "PRODUCTION_MODE", False)   # вне прод-режима демона
    assert notify.send_pending_once() == 0
    assert tg.messages == []
    assert rows(notify)[0]["status"] == "pending"   # не потеряно, просто молчит


def test_sender_loop_is_silent_when_not_production(tmp_path, monkeypatch, tg, notify):
    """Живой цикл, стартовавший вне прода, не делает ни одного вызова TG."""
    monkeypatch.setattr(notify, "PRODUCTION_MODE", False)
    add_order(notify, "ord_real")
    notify.enqueue("order.error", "T", "b", "e", order_id="ord_real")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(notify.sender_loop(interval=0.01, stop=stop))
        await asyncio.sleep(0.08)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert tg.messages == []


def test_production_sender_sends_filtered(notify, tg, monkeypatch):
    """В проде sender шлёт: настоящая ошибка доставлена, тестовый заказ — нет."""
    monkeypatch.setattr(notify, "PRODUCTION_MODE", True)
    add_order(notify, "ord_real", steps=[{"id": "s1", "type": "opencode",
                                         "state": "failed", "error_class": "runtime"}])
    add_order(notify, "ord_test", repo="/matrix/bench-pilot/repo",
              steps=[{"id": "s1", "type": "mock.ok"}])
    notify.enqueue("order.error", "T", "<b>real</b>", "e1", order_id="ord_real", event_id=1)
    notify.enqueue("order.error", "T", "<b>test</b>", "e2", order_id="ord_test", event_id=2)
    assert notify.send_pending_once() == 1
    assert len(tg.messages) == 1 and "real" in tg.messages[0]["text"]


# ── дедуп order+transition и лимит ───────────────────────────
def test_same_transition_repeated_is_coalesced(notify, tg):
    """Pause/resume-цикл (и одни и те же done-события) в verbose -> одно сообщение."""
    notify.MODE = "verbose"
    add_order(notify, "ord_real")
    sent = 0
    for i in range(10):
        # разные dedup-ключи, одинаковые данные перехода: order+transition
        sent += notify.enqueue("order.paused", "T", "b", f"p{i}", order_id="ord_real",
                               event_data_json='{"from": "active", "to": "paused"}')
        sent += notify.enqueue("order.resumed", "T", "b", f"r{i}", order_id="ord_real",
                               event_data_json='{"from": "paused", "to": "active"}')
    assert sent == 2                                   # по одному на переход
    assert notify.send_pending_once() == 2
    assert len(tg.messages) == 2


def test_legacy_flood_in_outbox_is_not_sent(notify, tg, monkeypatch):
    """Строки, оставшиеся от старого кода (2055 sent / pending-флуд), не шлются:
    каждый заказ перепроверяется на классификацию перед отправкой."""
    add_order(notify, "ord_bench", repo="/matrix/bench-pilot/repo",
              steps=[{"id": "s1", "type": "mock.ok"}])
    notify._allow_nonproduction_send = True
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        c.execute("INSERT INTO notifications(kind, order_id, step_id, title, body,"
                  " dedup_key, created, status) VALUES('order.done','ord_bench','s1',"
                  " 'T','b','legacy-1',?, 'pending')", (time.time(),))
    # демон — прод-сценарий; заказ тестовый
    monkeypatch.setattr(notify, "PRODUCTION_MODE", True)
    assert notify.send_pending_once() == 0
    assert tg.messages == []


def test_rate_limit_keeps_all_critical_not_lost(notify, tg):
    """10 апрувов + 10 done: апрувы (критические) уходят все, done — по лимиту."""
    for i in range(10):
        add_order(notify, f"ord_a{i}")
        add_approval(notify, f"apr_rate{i:08d}", f"ord_a{i}")
        notify.enqueue("approval.requested", "T", "q", f"apr:{i}",
                       order_id=f"ord_a{i}", approval_id=f"apr_rate{i:08d}")
        add_order(notify, f"ord_d{i}")
        notify.enqueue("order.done", "T", "b", f"done:{i}", order_id=f"ord_d{i}")
    notify.send_pending_once()
    sent = [r["kind"] for r in rows(notify, "SELECT kind FROM notifications WHERE status='sent'")]
    assert sent.count("approval.requested") == 10          # ни один апрув не потерян
    assert sent.count("order.done") < 10                   # done придержаны
    pend = rows(notify, "SELECT kind FROM notifications WHERE status='pending'")
    assert {p["kind"] for p in pend} == {"order.done"}     # остались как pending


def test_enqueue_never_raises_on_broken_orders_table(notify, tmp_path, monkeypatch):
    db = str(tmp_path / "weird.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE orders (id TEXT)")     # без нужных колонок
    monkeypatch.setattr(notify, "JOURNAL_DB", db)
    assert notify.enqueue("order.error", "T", "b", "k", order_id="x") is False


# ── API: однократность и понятная просрочка ──────────────────
async def test_api_approve_reject_once(db, notify):
    """Ровно один претендент применяет решение; второй получает состояние."""
    from src import actions
    from src.states import OrderState
    with sqlite3.connect(db.path) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
          step_id TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
          default_action TEXT NOT NULL DEFAULT 'wait', expires_at REAL,
          created REAL NOT NULL, resolved REAL);""")
        c.execute("INSERT INTO orders(id,goal,priority,repo,state,spec_json,created,updated)"
                  " VALUES('ord_x','g',0,'/matrix/dirigent','waiting_human',"
                  "'{\"steps\":[{\"id\":\"s1\",\"type\":\"opencode\","
                  "\"approval\":{\"mode\":\"after\"}}]}',?,?)", (time.time(), time.time()))
        c.execute("INSERT INTO steps(order_id,id,state,type,spec_json,updated)"
                  " VALUES('ord_x','s1','waiting_approval','opencode',"
                  "'{\"id\":\"s1\",\"type\":\"opencode\",\"approval\":{\"mode\":\"after\"}}',?)",
                  (time.time(),))
        c.execute("INSERT INTO approvals(id,order_id,step_id,question,created,expires_at)"
                  " VALUES('apr_once00000001','ord_x','s1','Принять?',?,?)",
                  (time.time(), time.time() + 3600))
    notify.JOURNAL_DB = db.path
    assert notify.approval_state("apr_once00000001") == "open"
    assert await actions.approve(db, "apr_once00000001") is True
    assert await actions.approve(db, "apr_once00000001") is False    # повтор
    assert await actions.reject(db, "apr_once00000001") is False     # после approve
    assert notify.approval_state("apr_once00000001") == "approved"


async def test_api_decision_states(client, db, notify, monkeypatch):
    """API отвечает state=expired/missing понятно и не падает (реальный журнал DB)."""
    monkeypatch.setattr(notify, "JOURNAL_DB", db.path)
    from tests.test_actions_api import submit
    oid = await submit(db, [
        {"id": "s1", "type": "mock.ok",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 3600}}])
    await db.create_approval(oid, "s1", "Принять?", "wait", 3600)
    cur = await db.conn.execute("SELECT id FROM approvals WHERE order_id=?", (oid,))
    aid = (await cur.fetchone())["id"]
    r = await client.post(f"/approvals/{aid}/approve")
    body = await r.json()
    assert r.status == 200 and body["ok"] is False and body["state"] == "approved"
    with sqlite3.connect(db.path) as c:   # апрув закрыт заранее как истёкший
        c.execute("UPDATE approvals SET status='open', expires_at=? WHERE id=?",
                  (time.time() - 1, aid))
    r = await client.post(f"/approvals/{aid}/reject")
    body = await r.json()
    assert body["ok"] is False and body["state"] == "expired"
    r = await client.post("/approvals/not_there/approve")
    body = await r.json()
    assert r.status == 404 and body["state"] == "missing"


# ── кнопки: данные из настоящей карточки ─────────────────────
DGT_RE = re.compile(r"^dgt:apr:(?P<aid>[^:]+):(?P<v>ok|no)$")


def _card_callback_datas(notify, tg, aid: str) -> list[str]:
    add_order(notify, "ord_btn")
    add_approval(notify, aid, "ord_btn")
    assert notify.enqueue("approval.requested", "T", "q", f"approval:{aid}",
                          order_id="ord_btn", approval_id=aid)
    assert notify.send_pending_once() == 1
    kb = json.loads(tg.messages[-1]["reply_markup"])["inline_keyboard"][0]
    return [b["callback_data"] for b in kb]


def test_outbox_card_callback_data_is_parseable(notify, tg):
    """Что отправил outbox — то бот и разбирает (инцидент: '|' vs ':'/фильтр)."""
    bd = _load_bot_dirigent()
    data = _card_callback_datas(notify, tg, "apr_btn000000001")
    assert data == ["dgt:apr:apr_btn000000001:ok", "dgt:apr:apr_btn000000001:no"]
    assert bd.parse_callback(data[0]) == ("apr_btn000000001", "ok")
    assert bd.parse_callback(data[1]) == ("apr_btn000000001", "no")


def _load_bot_dirigent():
    from pathlib import Path as _P
    import importlib.util
    import sys
    if not _P("/matrix/telegram-bot/bot_dirigent.py").exists():
        pytest.skip("bot_dirigent недоступен")
    for p in ("/usr/local/lib/python3.10/dist-packages",):
        if _P(p).is_dir() and p not in sys.path:
            sys.path.append(p)
    if importlib.util.find_spec("telegram") is None:
        pytest.skip("python-telegram-bot недоступен")
    if "/matrix/telegram-bot" not in sys.path:
        sys.path.insert(0, "/matrix/telegram-bot")
    sys.modules.pop("bot_dirigent", None)
    import bot_dirigent  # noqa: E402
    return bot_dirigent


def test_foreign_or_garbage_callback_rejected():
    """Карточки чужих ботов/устаревшие форматы не попадают в Дирижёра."""
    bd = _load_bot_dirigent()
    for data in ("aiops:apply:sig", "aig:g:sh0:1", "dgt:apr:..:ok:", "dgt:apr:xn",
                 "dgt:prune:1", "", None):
        assert bd.parse_callback(data) is None


def test_bot_regex_matches_every_generated_callback(notify):
    """Регэксп регистрации обработчика (bot_dirigent) принимает все наши кнопки."""
    pattern = re.compile(r"^dgt[:|]")
    for aid in ("apr_" + "a" * 12, "apr_" + "b" * 40):
        for d in notify.approval_keyboard(aid):
            assert pattern.search(d["callback_data"])
            assert DGT_RE.match(d["callback_data"])
