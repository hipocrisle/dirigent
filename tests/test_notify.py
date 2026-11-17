"""P2/P4: outbox уведомлений — тихий прод, фильтр тестов, дедуп, TTL, апрувы.

Регрессии к инциденту 17.11 (десятки сообщений за минуту от тестовых заказов,
дубли done/error, пауза/возобновление в TG, пустые approval '([])' TTL 1 мин).
"""
from __future__ import annotations

import sqlite3
import time

from tests.notify_fixtures import (add_approval, add_order, enqueue_approval, rows)


# ── базовое ──────────────────────────────────────────────────
def test_enqueue_and_dedup(notify):
    add_order(notify, "ord_real1")
    assert notify.enqueue("order.done", "T", "body", "k1", order_id="ord_real1")
    assert not notify.enqueue("order.done", "T", "body", "k1", order_id="ord_real1")  # дубль
    assert notify.enqueue("order.error", "T", "b", "k2", order_id="ord_real1")        # другой ключ
    assert len(rows(notify)) == 2


def test_default_mode_is_quiet(notify):
    """Прод-дефолт: тихий режим, даже если режим не выставлен явно."""
    import importlib
    m = importlib.reload(notify)
    assert m.MODE == "quiet"
    assert m.is_enabled("approval.requested")
    assert m.is_enabled("order.error")
    assert m.is_enabled("order.done")
    assert not m.is_enabled("order.paused")
    assert not m.is_enabled("order.resumed")


def test_transitions_not_sent_in_quiet(notify):
    add_order(notify, "ord_real1")
    assert not notify.enqueue("order.paused", "T", "b", "p", order_id="ord_real1")
    assert not notify.enqueue("order.resumed", "T", "b", "r", order_id="ord_real1")
    assert rows(notify) == []


# ── фильтр тестовых/служебных заказов по явным метаданным ────
def test_bench_pilot_orders_are_silent_but_ui_gets_events(notify):
    add_order(notify, "ord_bench", repo="/matrix/bench-pilot/repo",
              steps=[{"id": "s1", "type": "opencode"}])
    assert notify.enqueue("order.done", "T", "b", "d", order_id="ord_bench") is False
    assert notify.enqueue("order.error", "T", "b", "e", order_id="ord_bench") is False
    assert notify.enqueue("order.paused", "T", "b", "p", order_id="ord_bench") is False
    assert rows(notify) == []
    # «UI/аудит» — отдельный канал: events пишется всегда (проверяем, что
    # фильтр не трогает аудит, а лишь не дублирует его в Telegram)
    assert notify.is_user_facing(notify.classify_order("/matrix/bench-pilot/repo")) is False


def test_mock_steps_mean_test_order_not_goal_name(notify):
    # формулировка содержит «test» — но это НЕ критерий
    add_order(notify, "ord_a", repo="/matrix/dirigent", goal="test test test",
              steps=[{"id": "s1", "type": "opencode"}])
    assert notify.order_classification("ord_a") == "real"
    assert notify.enqueue("order.done", "T", "b", "d", order_id="ord_a")
    # тестовый адаптер ($type=mock.*) — тестовое событие, молчим
    add_order(notify, "ord_b", repo="/matrix/dirigent", goal="реальный текст",
              steps=[{"id": "s1", "type": "mock.ok"}])
    assert notify.order_classification("ord_b") == "test"
    assert notify.enqueue("order.done", "T", "b", "d2", order_id="ord_b") is False


def test_self_check_and_kill_guard_are_service(notify):
    add_order(notify, "ord_sc", repo=None, goal="kill-guard",
              steps=[{"id": "s1", "type": "shell", "danger": True}])
    assert notify.order_classification("ord_sc") in ("service", "test")
    assert not notify.enqueue("order.error", "T", "b", "e", order_id="ord_sc")
    add_order(notify, "ord_cons", repo="/matrix/dirigent",
              steps=[{"id": "s1", "type": "opencode"}],
              constraints={"test": True, "test_via": "self_check"})
    assert notify.order_classification("ord_cons") == "service"
    assert not notify.enqueue("order.done", "T", "b", "d", order_id="ord_cons")


def test_flood_of_30_test_events_sends_nothing(notify, tg):
    """30 тестовых событий (заказы 'test'/'t'/kill-guard, bench-pilot, mock) —
    владельцу НОЛЬ сообщений, демон не падает, процесс не блокируется."""
    orders = ["ord_test", "ord_t", "ord_killguard"]
    add_order(notify, "ord_test", repo="/matrix/bench-pilot/repo",
              goal="test", steps=[{"id": "s1", "type": "mock.ok"}])
    add_order(notify, "ord_t", repo=None, goal="t",
              steps=[{"id": "s1", "type": "mock.ok"}])
    add_order(notify, "ord_killguard", repo=None, goal="kill-guard",
              steps=[{"id": "s1", "type": "kill-guard", "danger": True}])
    kinds = ["order.done", "order.error", "order.paused", "order.resumed"]
    sent_intents = 0
    for i in range(30):
        oid = orders[i % len(orders)]
        k = kinds[i % len(kinds)]
        if notify.enqueue(k, "T", "b", f"flood-{i}", order_id=oid):
            sent_intents += 1
    assert sent_intents == 0
    assert notify.send_pending_once() == 0
    assert tg.messages == []
    assert rows(notify) == []


def test_real_order_with_test_word_in_goal_is_not_filtered(notify, tg):
    add_order(notify, "ord_real", repo="/matrix/dirigent",
              goal="исправить test-регрессии и запустить тесты",
              steps=[{"id": "s1", "type": "opencode", "state": "completed"}],
              priority=2)
    assert notify.enqueue("order.done", "T", "b", "d", order_id="ord_real")
    assert notify.send_pending_once() == 1


# ── дедуп и coalescing ───────────────────────────────────────
def test_dedup_by_event_id(notify):
    add_order(notify, "ord_real")
    assert notify.enqueue("order.error", "T", "b", "a", order_id="ord_real", event_id=77)
    # то же событие повторно (разные dedup-ключи, один event id) — дубль
    assert not notify.enqueue("order.error", "T", "b", "b", order_id="ord_real", event_id=77)
    # другое событие того же заказа — отдельное уведомление
    assert notify.enqueue("order.error", "T", "b", "c", order_id="ord_real", event_id=78)
    assert len(rows(notify)) == 2


def test_coalesce_30_transitions_one_message(notify, tg):
    add_order(notify, "ord_real")
    # даже в verbose (иначе тир фильтруется) переходы схлопываются
    notify.MODE = "verbose"
    got = sum(1 for i in range(30)
              if notify.enqueue("order.paused", "T", "b", f"p{i}", order_id="ord_real",
                                coalesce=True))
    assert got == 1
    assert notify.send_pending_once() == 1
    assert len(tg.messages) == 1


def test_rate_limit_delays_but_never_drops_approvals(notify, tg):
    """Лимит 5/мин: апрув уходит первым (приоритет), затем 4 done; 2 done
    отложены (next_retry) — не потеряны, доедут в следующих проходах."""
    notify.MODE = "verbose"
    for i in range(6):
        oid = f"ord_d{i}"
        add_order(notify, oid)
        notify.enqueue("order.done", "T", "b", f"d{i}", order_id=oid)
    add_order(notify, "ord_apr")
    add_approval(notify, "apr_000000000001", "ord_apr")
    enqueue_approval(notify, "apr_000000000001", "ord_apr")
    assert notify.send_pending_once() == 5
    pend = rows(notify, "SELECT kind FROM notifications WHERE status='pending'")
    assert [p["kind"] for p in pend] == ["order.done", "order.done"]   # отложены, не потеряны
    assert all(p["kind"] == "order.done" for p in pend)
    sent_kinds = [r["kind"] for r in rows(notify, "SELECT kind FROM notifications"
                                                   " WHERE status='sent'")]
    assert "approval.requested" in sent_kinds          # апрув доставлен первым
    assert sent_kinds.count("order.done") == 4
    assert tg.messages[0]["text"].startswith("<b>🙅")  # первым ушёл апрув
    # отложенные доходят после истечения лимита (next_retry в будущем)
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        c.execute("UPDATE notifications SET next_retry=0 WHERE status='pending'")
        c.execute("UPDATE notifications SET sent_at=NULL")   # окно лимита пустеет
    assert notify.send_pending_once() == 2


def test_sender_backoff_on_failure(notify, tg):
    add_order(notify, "ord_real")
    notify.enqueue("order.error", "T", "b", "k1", order_id="ord_real")
    tg.fail = True
    assert notify.send_pending_once() == 0
    r = rows(notify)[0]
    assert r["status"] == "pending" and r["attempts"] == 1 and r["next_retry"] > 0


def test_telegram_4xx_does_not_retry_forever(notify, tg):
    add_order(notify, "ord_real")
    notify.enqueue("order.error", "T", "b", "k1", order_id="ord_real")
    tg.reject_4xx = True
    assert notify.send_pending_once() == 0
    r = rows(notify)[0]
    assert r["status"] == "failed"            # не крутится бесконечно


# ── реальные критические сообщения ───────────────────────────
def test_real_critical_error_and_done_are_sent(notify, tg):
    add_order(notify, "ord_err", steps=[{"id": "s1", "type": "opencode",
                                         "state": "failed", "error_class": "runtime",
                                         "attempts": 2, "max_attempts": 2}])
    add_order(notify, "ord_ok", steps=[{"id": "s1", "type": "opencode",
                                        "state": "completed"}])
    assert notify.enqueue("order.error", "T", "b", "e", order_id="ord_err")
    assert notify.enqueue("order.done", "T", "d", "d", order_id="ord_ok")
    assert notify.send_pending_once() == 2
    assert len(tg.messages) == 2


# ── апрувы: только реальные, полезные, с живым TTL ───────────
def test_approval_with_buttons_sent_with_working_callback_data(notify, tg):
    add_order(notify, "ord_real")
    add_approval(notify, "apr_abcdef123456", "ord_real")
    assert enqueue_approval(notify, "apr_abcdef123456", "ord_real")
    assert notify.send_pending_once() == 1
    msg = tg.messages[0]
    import json
    kb = json.loads(msg["reply_markup"])["inline_keyboard"][0]
    datas = [b["callback_data"] for b in kb]
    assert datas == ["dgt:apr:apr_abcdef123456:ok", "dgt:apr:apr_abcdef123456:no"]
    assert all(len(d.encode()) <= 64 for d in datas)   # лимит Telegram
    assert "TTL:" in msg["text"] and "мин" in msg["text"]


def test_empty_approval_and_decided_approvals_not_sent(notify, tg):
    add_order(notify, "ord_real")
    assert not notify.is_meaningful_approval("s1", [], None)
    assert not notify.is_meaningful_approval("s1", {}, [])
    assert notify.is_meaningful_approval("s1", ["patch.diff"], None)
    # апрув уже решён/просрочен — не отправляем мёртвую карточку
    add_approval(notify, "apr_done00000001", "ord_real", status="approved")
    assert not enqueue_approval(notify, "apr_done00000001", "ord_real")
    add_approval(notify, "apr_exp000000001", "ord_real", ttl=-5)
    assert not enqueue_approval(notify, "apr_exp000000001", "ord_real")
    assert notify.send_pending_once() == 0
    assert tg.messages == []


def test_approval_not_sent_for_test_order(notify, tg):
    add_order(notify, "ord_bench", repo="/matrix/bench-pilot/repo",
              steps=[{"id": "s1", "type": "mock.approve"}])
    add_approval(notify, "apr_bench0000001", "ord_bench")
    assert not enqueue_approval(notify, "apr_bench0000001", "ord_bench")
    assert tg.messages == []


def test_approval_expiring_before_send_is_skipped(notify, tg):
    add_order(notify, "ord_real")
    add_approval(notify, "apr_fast00000001", "ord_real", ttl=0.05)
    assert enqueue_approval(notify, "apr_fast00000001", "ord_real")
    time.sleep(0.1)
    assert notify.send_pending_once() == 0
    assert tg.messages == []
    assert rows(notify)[0]["status"] == "skipped"


def test_approval_ttl_policy_never_one_minute(notify):
    # заявленный «1 мин» из случайного теста — не делает кнопки мёртвыми
    assert notify.effective_approval_ttl(60) == 3600
    assert notify.effective_approval_ttl(None) == 3600
    assert notify.effective_approval_ttl(7200) == 7200
    assert notify.approval_ttl_label(3600) == "1 ч"
    assert notify.approval_ttl_label(0) == "срок истёк"


def test_approval_card_ttl_reflects_journal_at_send_time(notify, tg):
    add_order(notify, "ord_real")
    add_approval(notify, "apr_ttl000000001", "ord_real", ttl=7200)
    enqueue_approval(notify, "apr_ttl000000001", "ord_real")
    notify.send_pending_once()
    text = tg.messages[0]["text"]
    assert "TTL:" in text
    assert "120 мин" in text or "2 ч" in text    # срок из журнала (7200 с)


def test_mark_decided_prevents_late_card(notify, tg):
    add_order(notify, "ord_real")
    add_approval(notify, "apr_late00000001", "ord_real")
    enqueue_approval(notify, "apr_late00000001", "ord_real")
    notify.mark_approval_decided("apr_late00000001", "approve")
    assert notify.send_pending_once() == 0
    assert tg.messages == []


def test_recover_after_restart_skips_stale(notify):
    add_order(notify, "ord_real")
    add_approval(notify, "apr_old000000001", "ord_real")
    enqueue_approval(notify, "apr_old000000001", "ord_real")
    with sqlite3.connect(notify.JOURNAL_DB) as c:
        c.execute("UPDATE approvals SET status='timeout' WHERE id='apr_old000000001'")
    assert notify.recover_after_restart() == 1
    assert rows(notify)[0]["status"] == "skipped"


def test_outbox_survives_missing_orders_table(notify, tmp_path, monkeypatch):
    """Fail-soft: нет журнала/таблиц — enqueue не бросает (демон не падает)."""
    db = str(tmp_path / "empty.db")
    monkeypatch.setattr(notify, "JOURNAL_DB", db)
    # реальный заказ не найден в (пустом) журнале -> не user-facing -> молчим
    assert notify.enqueue("order.error", "T", "b", "k", order_id="nope") is False
