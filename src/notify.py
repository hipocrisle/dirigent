"""P2/P4: Telegram-уведомления через transactional outbox (docs/P2-telegram-spec.md).

Дирижёр пишет намерение уведомления в SQLite (notifications, dedup_key UNIQUE) —
это переживает рестарт демона и недоступность Telegram. Sender-цикл доставляет
через локальный bot API (127.0.0.1:8081), backoff при сбоях, дубли отсекаются
уникальным ключом. Заказы НИКОГДА не блокируются недоступностью TG.

P4 «тихий прод» (инцидент 17.11: десятки сообщений в минуту):
  * production quiet-by-default: владельцу уходят только реальные апрувы,
    критические ошибки и завершение важных РЕАЛЬНЫХ заказов;
  * тестовые/служебные заказы (test/mock/bench-pilot/self-check) определяются
    по ЯВНЫМ метаданным (repo, заголовок типа шага, состояние, метка в spec,
    признак danger self-check) — не по вхождению 'test' в формулировку цели;
  * dedup по event_id и order+transition внутри окна, rate-limit не блокирует
    демон и не теряет апрувы;
  * апрув без действий (пустой результат/нечего принимать) не отправляется.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("dirigent.notify")

JOURNAL_DB = "/matrix/dirigent/dirigent.db"
TG_API = "http://127.0.0.1:8081"
ADMIN_CHAT = 424106681  # ADMIN_ID из telegram-bot/bot.py
_DEFAULT_JOURNAL_DB = JOURNAL_DB

# kind -> тир доставки. quiet (дефолт): approval/critical_done/critical_error.
TIERS = {
    "approval.requested": "approval",
    "order.done": "critical_done",
    "order.error": "critical_error",
    "step.retry_refused": "critical_error",
    "order.paused": "transition",
    "order.resumed": "transition",
}
MODES = {
    "quiet": {"approval", "critical_done", "critical_error"},
    "normal": {"approval", "critical_done", "critical_error"},
    "verbose": {"approval", "critical_done", "critical_error", "transition"},
}
MODE = "quiet"          # production по умолчанию; verbose — только для разбора
# Производственный режим процесса, зафиксированный при импорте ДО того, как
# тесты могли подменить MODE/JOURNAL_DB (sender_loop живого демона не должен
# слать ничего, если его среда переопределена; инцидент 17.11 — флуд из тестов).
PRODUCTION_MODE = (MODE == "quiet" and JOURNAL_DB == _DEFAULT_JOURNAL_DB)

KIND_TITLES = {
    "approval.requested": "🙅 Требуется решение",
    "order.done": "✅ Заказ завершён",
    "order.error": "❌ Заказ провалился",
    "step.retry_refused": "⛔ Попытки исчерпаны",
    "order.paused": "⏸ Заказ на паузе",
    "order.resumed": "▶️ Заказ продолжён",
}
# Переходы, которые в quiet-режиме остаются в UI/аудите (таблица events), без TG.
UI_ONLY_KINDS = frozenset({"order.paused", "order.resumed"})

# ── классификация заказа (явные метаданные, не имя цели) ───────
TEST_REPO_PREFIXES = ("/matrix/bench-pilot",)
SERVICE_REPO_PREFIXES = ("/matrix/dirigent-selfcheck",)
# Заголовок типа шага, а не имя шага: mock.* — тестовый адаптер.
TEST_STEP_TYPE_PREFIXES = ("mock.", "bench.", "selfcheck.")
# Тестовые/служебные метки в spec.constraints (явные метаданные создателя заказа).
TEST_FLAGS = frozenset({"kill_guard", "kill-guard", "self_check", "self-check",
                        "selfcheck", "bench_pilot", "bench-pilot", "test"})
SELF_CHECK_FLAGS = frozenset({"kill_guard", "kill-guard", "self_check", "self-check",
                              "selfcheck"})
# Точные имена служебных заказов (self-check демона/смоук-скриптов).
SERVICE_GOAL_NAMES = ("kill-guard", "killguard", "self-check", "selfcheck", "healthcheck")
DEFAULT_TTL_SECONDS = 3600.0
# TTL апрува: короче 5 минут кнопки становятся мёртвыми раньше, чем владелец
# их увидит (инцидент «TTL 1 мин» 17.11). Всё, что меньше — дефолт.
MIN_APPROVAL_TTL_SECONDS = 300.0

# Дедуп/лимиты (только для НЕ-апрувов: апрувы не теряем никогда).
COALESCE_WINDOW_SECONDS = 600      # одинаковый order+transition внутри окна -> одно сообщение
RATE_MAX_PER_MINUTE = 5            # не больше N уведомлений владельцу в минуту
RATE_WINDOW_SECONDS = 60.0
CRITICAL_KINDS = frozenset({"approval.requested", "order.error", "step.retry_refused"})


# ── storage ───────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(JOURNAL_DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:  # не блокируем демон на записи; при занятости — fail-soft
        c.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
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
    for ddl in ("ALTER TABLE notifications ADD COLUMN tier TEXT",
                "ALTER TABLE notifications ADD COLUMN event_id INTEGER",
                "ALTER TABLE notifications ADD COLUMN expires_at REAL",
                "ALTER TABLE notifications ADD COLUMN decided TEXT",
                "ALTER TABLE notifications ADD COLUMN event_json TEXT"):
        try:
            c.execute(ddl)  # миграция старых БД; дубль колонки игнорируем
        except sqlite3.OperationalError:
            pass
    try:
        c.commit()
    except Exception:
        pass
    return c


def current_mode() -> str:
    return MODE if MODE in MODES else "quiet"


def is_enabled(kind: str) -> bool:
    tier = TIERS.get(kind, "transition")
    return tier in MODES.get(current_mode(), MODES["quiet"])


# ── классификация ────────────────────────────────────────────
def _spec_constraints(spec_json: str | None) -> dict:
    try:
        d = json.loads(spec_json or "{}")
    except Exception:
        return {}
    c = d.get("constraints")
    return c if isinstance(c, dict) else {}


def _goal_marks_service(goal: str | None) -> bool:
    """Служебный self-check, если создатель явно так назвал заказ.

    Это НЕ фильтр по вхождению 'test': сравнивается точное имя служебной
    проверки (kill-guard/self-check/healthcheck), которым штампуют свои заказы
    smoke-скрипты и ночные self-check демона.
    """
    g = (goal or "").strip().lower()
    return g in SERVICE_GOAL_NAMES or g.startswith(tuple(f"{n}:" for n in SERVICE_GOAL_NAMES))


def classify_order(repo: str | None, steps: list[dict] | None = None,
                   spec_json: str | None = None, goal: str | None = None) -> str:
    """'real' | 'test' | 'service' по явным метаданным.

    steps: [{"type": ..., "danger": bool}] — типы/заголовки адаптеров.
    Цель заказа проверяется только на ТОЧНОЕ служебное имя (kill-guard и т.п.):
    'test' внутри формулировки не считается признаком теста.
    """
    r = (repo or "").rstrip("/")
    if any(r == p or r.startswith(p + "/") for p in SERVICE_REPO_PREFIXES):
        return "service"
    if any(r == p or r.startswith(p + "/") for p in TEST_REPO_PREFIXES):
        return "test"
    if _goal_marks_service(goal):
        return "service"
    c = _spec_constraints(spec_json)
    via = str(c.get("test_via") or "").strip().lower()
    if c.get("test") is True:
        # явная пометка создателя заказа: self-check/kill-guard — служебное
        return "service" if via in SELF_CHECK_FLAGS else "test"
    if str(c.get("in_scope") or "").strip().lower() in TEST_FLAGS:
        return "test"
    if c.get("self_check") is True:
        return "service"
    for s in steps or []:
        t = (s.get("type") or "").lower()
        if t.startswith(TEST_STEP_TYPE_PREFIXES):
            return "test"
        if s.get("danger") is True and t.startswith(("kill-guard", "self-check", "selfcheck")):
            return "service"
    return "real"


def is_user_facing(classification: str | None) -> bool:
    """Владельцу показываем только реальные заказы."""
    return classification == "real"


def order_meta_from_db(order_id: str | None) -> dict:
    """Метаданные заказа из журнала: repo, goal, spec_json, типы/опасность шагов."""
    if not order_id:
        return {"repo": None, "goal": None, "spec_json": None, "steps": []}
    try:
        with _conn() as c:
            o = c.execute("SELECT repo, goal, spec_json FROM orders WHERE id=?",
                          (order_id,)).fetchone()
            steps = [dict(r) for r in c.execute(
                "SELECT id, type, spec_json FROM steps WHERE order_id=?",
                (order_id,)).fetchall()]
    except Exception:
        return {"repo": None, "goal": None, "spec_json": None, "steps": []}
    out_steps = []
    for s in steps:
        sj = s.get("spec_json")
        danger = False
        try:
            danger = bool(json.loads(sj or "{}").get("danger"))
        except Exception:
            pass
        out_steps.append({"id": s["id"], "type": s.get("type"), "danger": danger})
    return {"repo": o["repo"] if o else None,
            "goal": o["goal"] if o else None,
            "spec_json": o["spec_json"] if o else None,
            "steps": out_steps}


def order_classification(order_id: str | None) -> str:
    m = order_meta_from_db(order_id)
    if order_id and not _order_exists(order_id):
        return "unknown"   # fail-closed: без метаданных владельцу не пишем
    return classify_order(m["repo"], m["steps"], m["spec_json"], m["goal"])


def _order_exists(order_id: str) -> bool:
    try:
        with _conn() as c:
            return c.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone() is not None
    except Exception:
        return False


def is_important_order(order_id: str | None) -> bool:
    """Важность для order.done: реальный заказ с реальным шагом, либо
    явно помеченный как важный (constraints.important / priority>=2)."""
    m = order_meta_from_db(order_id)
    if not is_user_facing(classify_order(m["repo"], m["steps"], m["spec_json"], m["goal"])):
        return False
    c = _spec_constraints(m["spec_json"])
    if c.get("important") is True:
        return True
    try:
        if int(json.loads(m["spec_json"] or "{}").get("priority") or 0) >= 2:
            return True
    except Exception:
        pass
    # Не-тестовый заказ с хотя бы одним не-mock шагом считаем важным.
    return any(not (s.get("type") or "").lower().startswith(TEST_STEP_TYPE_PREFIXES)
               for s in m["steps"])


# ── апрув: полезность/TTL ────────────────────────────────────
def effective_approval_ttl(spec_ttl: float | None) -> float:
    """TTL апрува в журнале. Заявленный берём, если он разумен (>= MIN):
    «1 мин» случайного теста не должен делать кнопки мёртвыми; совсем без
    значения — часовой дефолт (молчание != согласие, TTL-политика явная)."""
    try:
        ttl = float(spec_ttl) if spec_ttl else 0.0
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl >= MIN_APPROVAL_TTL_SECONDS:
        return ttl
    return DEFAULT_TTL_SECONDS


def approval_ttl_seconds(expires_at: float | None, now: float | None = None) -> float:
    """Сколько ещё живёт апрув. Нет TTL/просрочен -> 0."""
    if expires_at is None:
        return 0.0
    return max(0.0, float(expires_at) - (now if now is not None else time.time()))


def approval_ttl_label(ttl: float) -> str:
    if ttl <= 0:
        return "срок истёк"
    if ttl >= 3600 and int(ttl) % 3600 == 0:
        return f"{int(ttl // 3600)} ч"
    if ttl >= 60:
        return f"{int(round(ttl / 60))} мин"
    return f"{int(ttl)} сек"


def _approval_expiry(approval_id: str | None) -> float | None:
    r = get_approval(approval_id or "")
    if not r:
        return None
    exp = r.get("expires_at")
    return float(exp) if exp is not None else None


def approval_card_body(approval_id: str, now: float | None = None) -> str:
    """Тело карточки апрува. TTL считается из журнала в момент отправки:
    карточка, пролежавшая в очереди, не врёт про срок."""
    now = now if now is not None else time.time()
    a = get_approval(approval_id)
    if not a:
        return ""
    exp = a.get("expires_at")
    ttl_line = "TTL: без ограничения"
    if exp is not None:
        exp = float(exp)
        ttl_line = (f"TTL: {approval_ttl_label(approval_ttl_seconds(exp, now))}"
                    f" (до {time.strftime('%d.%m %H:%M', time.localtime(exp))})")
    return (f"Заказ: <code>{a['order_id']}</code>\n"
            f"Шаг: <code>{a['step_id']}</code>\n"
            f"{(a.get('question') or '').strip()}\n"
            f"{ttl_line}\n"
            f"Кнопки ниже или /approve {approval_id} / /reject {approval_id}")


def approval_outcome_message(state: str, verdict: str | None = None) -> tuple[str, bool]:
    """(текст, alert) для ответа на кнопку: понятно, что уже поздно/сделано."""
    v = {"ok": "разрешение", "no": "отклонение"}.get(verdict or "", "решение")
    if state == "open":
        return f"✅ {v.capitalize()} принято", False
    if state == "expired":
        return ("⌛ Срок решения истёк — апрув больше не активен.\n"
                "Решение по умолчанию уже применено; посмотрите /dapprovals."), True
    if state == "missing":
        return "⚠️ Апрув не найден в журнале Дирижёра.", True
    if state == "timeout":
        return "⌛ Апрув истёк по TTL (решение по умолчанию применено).", True
    if state in ("approved", "rejected"):
        return (f"ℹ️ Этот апрув уже {'разрешён' if state == 'approved' else 'отклонён'} — "
                "повторное решение не применяется."), True
    return f"⚠️ Апрув в состоянии '{state}', решение не применено.", True


def is_meaningful_approval(step_id: str, artifacts: dict | list | None,
                           actions: list | None) -> bool:
    """Есть ли что решать. Приёмка результата с пустым списком артефактов и без
    действий — бессмысленна (инцидент 'Принять результат? ([])'): одно «Принять»
    без последствий, кнопки не нужны."""
    acts = [a for a in (actions or []) if str(a).strip()]
    if acts:
        return True
    if isinstance(artifacts, dict):
        return bool(artifacts)
    return bool(list(artifacts or []))


def get_approval(approval_id: str) -> dict | None:
    try:
        with _conn() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def approval_state(approval_id: str) -> str:
    """'open' | 'expired' | 'approved' | 'rejected' | 'timeout' | 'missing'."""
    r = get_approval(approval_id)
    if not r:
        return "missing"
    if r["status"] == "open":
        exp = r.get("expires_at")
        if exp is not None and float(exp) < time.time():
            return "expired"
        return "open"
    return str(r["status"])


def _should_send_approval(approval_id: str | None, order_id: str | None) -> bool:
    if not approval_id:
        return False
    if not is_user_facing(order_classification(order_id)):
        return False
    if approval_state(approval_id) != "open":
        return False
    # карточка без единой рабочей кнопки не нужна никому
    return bool(approval_keyboard(str(approval_id)))


def _signature(data_json: str | None) -> str | None:
    """Нормализованная подпись данных события (order+transition), либо None."""
    if not data_json:
        return None
    try:
        d = json.loads(data_json)
    except Exception:
        return (data_json or "")[:200] or None
    if isinstance(d, dict):
        d = {k: d[k] for k in sorted(d) if k not in ("ts", "id", "seq", "approval")}
    return json.dumps(d, sort_keys=True, ensure_ascii=False)[:200]


def enqueue(kind: str, title: str, body: str, dedup_key: str,
            order_id: str | None = None, step_id: str | None = None,
            approval_id: str | None = None, event_id: int | None = None,
            expire_seconds: float | None = None,
            coalesce: bool = False, event_data_json: str | None = None) -> bool:
    """Идемпотентно. False — дубль/не тир/тестовый заказ/не актуально.

    dedup: UNIQUE(dedup_key) + явная проверка event_id (тот же вид/заказ/шаг) +
    подпись данных события (order+transition) в окне COALESCE_WINDOW_SECONDS.
    coalescing: одинаковый order+transition в окне -> одно сообщение; строка
    резервируется в той же транзакции (claim), поэтому флуд 30 событий даёт 1.
    Апрувы и критические ошибки НЕ отбрасываются coalescing'ом и rate-limit'ом.
    """
    if kind not in TIERS or not is_enabled(kind):
        return False
    # тестовые/служебные заказы владельцу не пишем вообще
    if not is_user_facing(order_classification(order_id)):
        return False
    critical = kind in CRITICAL_KINDS
    expires = None
    if kind == "approval.requested":
        if not _should_send_approval(approval_id, order_id):
            return False
        exp = _approval_expiry(approval_id)
        if exp is not None and exp < time.time():
            return False  # апрув уже истёк — карточку с мёртвыми кнопками не шлём
        expires = exp if exp is not None else (
            time.time() + (expire_seconds or DEFAULT_TTL_SECONDS))
        body = redact_progress(body)
    elif kind == "order.done" and not is_important_order(order_id):
        return False
    try:
        now = time.time()
        key = f"ev:{event_id}" if event_id is not None else dedup_key
        with _conn() as c:
            if event_id is not None:
                # Один event_id — одно уведомление на вид события. Сюда же
                # попадают «слившиеся» строки coalescing с тем же event_id.
                same = c.execute(
                    "SELECT 1 FROM notifications WHERE event_id=? AND kind=? AND"
                    " IFNULL(order_id,'')=IFNULL(?,'') AND IFNULL(step_id,'')=IFNULL(?,'')",
                    (event_id, kind, order_id, step_id)).fetchone()
                if same:
                    return False
            if event_data_json is not None and not critical:
                # order+transition: тот же переход в том же заказе (pause/resume
                # цикл, повторная ошибка одного шага) — одно уведомление на окно.
                sig = _signature(event_data_json) or ""
                dsame = c.execute(
                    "SELECT 1 FROM notifications WHERE kind=? AND IFNULL(order_id,'')=IFNULL(?,'')"
                    " AND IFNULL(event_json,'')=? AND created>=?",
                    (kind, order_id, sig, now - COALESCE_WINDOW_SECONDS)
                ).fetchone()
                if dsame:
                    return False
            if coalesce and not critical:
                cur = c.execute(
                    "INSERT OR IGNORE INTO notifications(kind, order_id, step_id, title,"
                    " body, dedup_key, created, tier, status, sent_at, event_id, event_json)"
                    " VALUES (?,?,?,?,?,?,?,?, 'merged', ?,?,?)",
                    (kind, order_id, step_id, title, "",
                     f"merge={kind}", now - (now % COALESCE_WINDOW_SECONDS),
                     TIERS[kind], now, event_id,
                     _signature(event_data_json) if event_data_json is not None else None))
                if cur.rowcount == 0:
                    return False  # окно уже занято — событие ушло в UI/аудит
            c.execute("INSERT INTO notifications(kind, order_id, step_id, approval_id,"
                      " title, body, dedup_key, created, tier, event_id, expires_at,"
                      " event_json)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (kind, order_id, step_id, approval_id, title, body[:900],
                       key, now, TIERS[kind], event_id, expires,
                       _signature(event_data_json) if event_data_json is not None else None))
        return True
    except sqlite3.IntegrityError:
        return False  # дубль — ок
    except Exception:
        log.exception("notify.enqueue soft-fail")
        return False  # уведомления не должны ломать заказы


def redact_progress(body: str) -> str:
    """Сырой dict прогресса {..'failed': 1..} заменить на читаемую сводку."""
    if "'failed'" in body or '"failed"' in body:
        body = body.split("\n{", 1)[0]
    return body


def mark_approval_decided(approval_id: str, verdict: str) -> None:
    """Кнопки в уже отправленных карточках этого апрува получены — не дублим."""
    try:
        with _conn() as c:
            c.execute("UPDATE notifications SET decided=? WHERE approval_id=? AND status='pending'",
                      (verdict, approval_id))
    except Exception:
        pass


def acknowledge_approval_cards(approval_id: str, verdict: str) -> int:
    """Решение принято вне кнопки (CLI, /approve, веб): гасим кнопки в уже
    доставленных карточках, чтобы не выглядело, будто решение ещё возможно.
    Возвращает число отредактированных сообщений. Никогда не бросает."""
    label = {"approve": "✅ уже разрешено", "reject": "🚫 уже отклонено",
             "approved": "✅ уже разрешено", "rejected": "🚫 уже отклонено"}.get(
                 verdict, "решение принято")
    n = 0
    rows = []
    try:
        with _conn() as c:
            rows = c.execute("SELECT tg_msg_id FROM notifications WHERE approval_id=?"
                             " AND tg_msg_id IS NOT NULL AND status='sent'",
                             (approval_id,)).fetchall()
    except Exception:
        return 0
    token = _bot_token()
    if not token or not rows:
        return 0
    exp = _approval_expiry(approval_id)
    for r in rows:
        _tg_edit_message_text(token, r["tg_msg_id"],
                              f"<b>{KIND_TITLES['approval.requested']}</b>\n\n"
                              f"Апрув <code>{approval_id}</code>: {label}."
                              + (f"\nСрок был до {time.strftime('%d.%m %H:%M', time.localtime(exp))}"
                                 if exp else ""))
        n += 1
    return n


# ── отправка ─────────────────────────────────────────────────
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


def _tg_edit_reply_markup(token: str, msg_id: int) -> None:
    """Снять кнопки с доставленной карточки (апрув решён)."""
    try:
        req = urllib.request.Request(
            f"{TG_API}/bot{token}/editMessageReplyMarkup",
            data=urllib.parse.urlencode({"chat_id": ADMIN_CHAT, "message_id": msg_id,
                                         "reply_markup": json.dumps({"inline_keyboard": []})}
                                        ).encode())
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _tg_answer_callback(token: str, callback_id: str, text: str,
                        alert: bool = False) -> None:
    """Гасим «часики» на кнопке — иначе Telegram считает callback незавершённым."""
    try:
        req = urllib.request.Request(
            f"{TG_API}/bot{token}/answerCallbackQuery",
            data=urllib.parse.urlencode({"callback_query_id": callback_id, "text": text[:190],
                                         "show_alert": "true" if alert else "false"}).encode())
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _tg_edit_message_text(token: str, msg_id: int, text: str) -> None:
    try:
        req = urllib.request.Request(
            f"{TG_API}/bot{token}/editMessageText",
            data=urllib.parse.urlencode({"chat_id": ADMIN_CHAT, "message_id": msg_id,
                                         "text": text[:4000], "parse_mode": "HTML",
                                         "reply_markup": json.dumps({"inline_keyboard": []})}
                                        ).encode())
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def approval_keyboard(approval_id: str) -> list:
    # < 64 байт: dgt:apr:<apr_xxxxxxxxxxxx>:ok
    return [
        {"text": "✅ Разрешить", "callback_data": f"dgt:apr:{approval_id}:ok"},
        {"text": "❌ Отклонить", "callback_data": f"dgt:apr:{approval_id}:no"},
    ]


def approval_keyboard_markup(approval_id: str) -> list:
    """reply_markup.inline_keyboard. Пустой список — кнопок не будет:
    идентификатор, не влезающий в callback_data Telegram (64 байта), дал бы
    карточку с «мёртвыми» кнопками, такую владельцу не шлём."""
    data = f"dgt:apr:{approval_id}"
    if not approval_id or len((data + ":ok").encode()) > 64:
        return []
    return [approval_keyboard(approval_id)]


def _rate_limited(c, now: float) -> bool:
    n = c.execute("SELECT COUNT(*) FROM notifications WHERE sent_at>?",
                  (now - RATE_WINDOW_SECONDS,)).fetchone()[0]
    return n >= RATE_MAX_PER_MINUTE


# Флаг для ручных экспериментов в не-прод окружении (тесты, стейджинг).
# Прод-демон его не выставляет; sender_loop его не трогает.
_allow_nonproduction_send = False


def send_pending_once() -> int:
    """Один проход отправки. -> число отправленных. Backoff: 5с/30с/300с.

    Порядок: сначала критические (апрувы/ошибки), затем остальное; rate-limit
    откладывает НЕ-критические (next_retry), критические не теряются.
    Не блокирует демон: sender живёт в отдельном потоке (asyncio.to_thread).
    """
    token = _bot_token()
    if not token:
        return 0
    # Консервативный предохранитель: вне прод-режима (тесты/подменённый журнал)
    # автоматический sender не шлёт вообще, даже если что-то попало в outbox.
    if not PRODUCTION_MODE and not _allow_nonproduction_send:
        return 0
    sent = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM notifications WHERE status='pending' AND next_retry<=?"
            " ORDER BY CASE kind WHEN 'approval.requested' THEN 0"
            "  WHEN 'order.error' THEN 1 WHEN 'step.retry_refused' THEN 1"
            "  WHEN 'order.done' THEN 2 ELSE 3 END, id LIMIT 20",
            (time.time(),)).fetchall()
        for r in rows:
            now = time.time()
            kind = r["kind"]
            critical = kind in CRITICAL_KINDS
            if kind == "approval.requested":
                if r["decided"] or approval_state(r["approval_id"] or "") != "open":
                    # решено/истекло/удалено: карточка с мёртвыми кнопками не нужна
                    c.execute("UPDATE notifications SET status='skipped' WHERE id=?", (r["id"],))
                    continue
            # последний рубеж: тестовый/служебный заказ не уходит, даже если строка
            # осталась в outbox от старого кода (инцидент 17.11)
            if not is_user_facing(order_classification(r["order_id"])):
                c.execute("UPDATE notifications SET status='skipped' WHERE id=?", (r["id"],))
                continue
            if not critical and _rate_limited(c, now):
                c.execute("UPDATE notifications SET next_retry=? WHERE id=?",
                          (now + RATE_WINDOW_SECONDS, r["id"]))
                continue
            body = r["body"]
            if kind == "approval.requested":
                # TTL/состояние — из журнала в момент отправки
                fresh = approval_card_body(r["approval_id"] or "", now)
                if fresh:
                    body = fresh
            msg_id = None
            try:
                kb = (approval_keyboard_markup(r["approval_id"])
                      if kind == "approval.requested" and r["approval_id"] else None)
                msg_id = _tg_send(token, f"<b>{r['title']}</b>\n\n{body}", kb)
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    # 4xx от Telegram: повтор не поможет (битый HTML/чат) — в failed
                    detail = ""
                    try:
                        detail = e.read().decode()[:300]
                    except Exception:
                        pass
                    log.warning("tg reject %s kind=%s: %s", e.code, kind, detail)
                    c.execute("UPDATE notifications SET status='failed', attempts=attempts+1"
                              " WHERE id=?", (r["id"],))
                    continue
                msg_id = None
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


def recover_after_restart() -> int:
    """После рестарта демона: закрыть неактуальные pending-апрувы.

    Апрув, чьё решение уже принято/истекло, не должен прийти владельцу
    «вчерашней» карточкой. Возвращает число закрытых записей.
    """
    n = 0
    try:
        with _conn() as c:
            rows = c.execute("SELECT id, approval_id FROM notifications"
                             " WHERE status='pending' AND kind='approval.requested'").fetchall()
            for r in rows:
                if approval_state(r["approval_id"] or "") != "open":
                    c.execute("UPDATE notifications SET status='skipped' WHERE id=?", (r["id"],))
                    n += 1
            c.commit()
    except Exception:
        pass
    return n


async def sender_loop(interval: float = 3.0, stop=None) -> None:
    import asyncio
    # Живой поток фиксирует прод-режим при СТАРТЕ: подмена MODE/JOURNAL_DB
    # после старта (тесты в том же процессе) не должна разворачивать доставку.
    production = PRODUCTION_MODE
    first = True
    while stop is None or not stop.is_set():
        try:
            if production:
                if first:
                    await asyncio.to_thread(recover_after_restart)
                    first = False
                await asyncio.to_thread(send_pending_once)
        except Exception:
            log.exception("notify sender soft-fail")
        await asyncio.sleep(interval)
