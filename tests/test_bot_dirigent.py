"""Регрессии Telegram-маршрутов Дирижёра (bot_dirigent) — кнопки, auth, TTL.

Инцидент 17.11: inline «Разрешить/Отклонить» не работали. Причины, которые
проверяются здесь:
  * callback_data был `dgt|apr|...`, а общий callback_handler бота (без
    фильтра, зарегистрирован ПОСЛЕ) не знал такого префикса;
  * callback не подтверждался (answer), пока шёл запрос, и при отказе прав;
  * проверка админа (`is_admin`) не импортировалась → NameError;
  * API-ответ («просрочено») не доходил до владельца понятным текстом.

Модуль требует python-telegram-bot (в проекте .venv его нет) — импортируется
системным интерпретатором. Если библиотеки нет, тесты скипаются.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

BOT_DIR = Path("/matrix/telegram-bot")
# python-telegram-bot установлен системно (в .venv его нет). Добавляем путь к
# dist-packages, чтобы тесты запускались из проектного окружения; если пакета
# нет вовсе — тесты честно скипаются.
for _p in ("/usr/local/lib/python3.10/dist-packages",):
    if Path(_p).is_dir() and _p not in sys.path:
        sys.path.append(_p)
HAVE_PTB = importlib.util.find_spec("telegram") is not None

pytestmark = pytest.mark.skipif(not HAVE_PTB or not BOT_DIR.exists(),
                                reason="python-telegram-bot / bot_dirigent недоступны")


# ── стенд: бот-модуль без сети ───────────────────────────────
class FakeQuery:
    def __init__(self, data: str, uid: int = 424106681):
        self.data = data
        self.from_user = types.SimpleNamespace(id=uid, username="admin")
        self.answers: list[dict] = []
        self.markup_edits: list = []
        self.message = types.SimpleNamespace(message_id=1, text="card")

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append({"text": text, "alert": show_alert})

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


class FakeMessage:
    def __init__(self, text: str):
        self.text = text
        self.sent: list[dict] = []

    async def reply_text(self, text: str, **kw):
        self.sent.append({"text": text, **kw})


def fake_update(data: str, uid: int = 424106681):
    q = FakeQuery(data, uid)
    u = types.SimpleNamespace(callback_query=q, effective_user=q.from_user,
                              message=None)
    return u


@pytest.fixture
def botmod(monkeypatch):
    """bot_dirigent с подменённым bot.is_admin (как в живом bot.py)."""
    sys.path.insert(0, str(BOT_DIR))
    allowed = {"424106681"}

    fake_bot = types.ModuleType("bot")

    def _is_admin(update):
        return str(update.effective_user.id) in allowed

    fake_bot.is_admin = _is_admin
    monkeypatch.setitem(sys.modules, "bot", fake_bot)
    sys.modules.pop("bot_dirigent", None)
    import bot_dirigent as m                      # noqa: E402
    m._allowed = allowed
    return m


def patch_api(monkeypatch, m, responses: dict):
    """POST /approvals/{aid}/{verb} -> предзаданный ответ API."""
    calls = []

    def fake_post_json(path):
        calls.append(path)
        return responses.get(path, {"ok": False, "state": "missing"})

    monkeypatch.setattr(m, "_post_json", fake_post_json)
    return calls


# ── callback_data / парсер ───────────────────────────────────
def test_callback_data_matches_notify_keyboard(botmod):
    """Кнопки, которые шлёт outbox, обязаны разбираться ботом."""
    import src.notify as notify
    kb = notify.approval_keyboard_markup("apr_abcdef123456")
    datas = [b["callback_data"] for b in kb[0]]
    assert datas == ["dgt:apr:apr_abcdef123456:ok", "dgt:apr:apr_abcdef123456:no"]
    assert botmod.parse_callback(datas[0]) == ("apr_abcdef123456", "ok")
    assert botmod.parse_callback(datas[1]) == ("apr_abcdef123456", "no")


def test_callback_legacy_pipe_format_still_parsed(botmod):
    """Старые карточки (P2, разделитель '|') продолжают работать."""
    assert botmod.parse_callback("dgt|apr|apr_old123:ok") == ("apr_old123", "ok")


@pytest.mark.parametrize("data", ["", "dgt:apr:x:maybe", "dgt:other:a:ok",
                                  "dgt:apr::ok", "text", None, "dgt:apr:a"])
def test_callback_garbage_rejected(botmod, data):
    assert botmod.parse_callback(data) is None


def test_callback_data_fits_telegram_limit(botmod):
    import src.notify as notify
    kb = notify.approval_keyboard_markup("apr_" + "f" * 20)
    for btn in kb[0]:
        assert len(btn["callback_data"].encode()) <= 64
    # слишком длинный ID -> кнопок нет вовсе (карточку не шлём), а не битые данные
    assert notify.approval_keyboard_markup("apr_" + "f" * 80) == []


# ── handler: ack всегда, однократное решение, понятная просрочка ──
async def test_callback_approve_acks_and_applies(botmod, monkeypatch):
    calls = patch_api(monkeypatch, botmod,
                      {"/approvals/apr_x/approve": {"ok": True, "state": "approved"}})
    u = fake_update("dgt:apr:apr_x:ok")
    await botmod.callback_dirigent(u, None)
    q = u.callback_query
    assert calls == ["/approvals/apr_x/approve"]
    assert q.answers and q.answers[0]["alert"] is False
    assert q.markup_edits == [None]          # кнопки сняты: повтор невозможен


async def test_callback_reject_uses_reject_endpoint(botmod, monkeypatch):
    calls = patch_api(monkeypatch, botmod,
                      {"/approvals/apr_y/reject": {"ok": True, "state": "rejected"}})
    u = fake_update("dgt:apr:apr_y:no")
    await botmod.callback_dirigent(u, None)
    assert calls == ["/approvals/apr_y/reject"]
    assert u.callback_query.answers[0]["alert"] is False


async def test_callback_expired_explains_and_alerts(botmod, monkeypatch):
    patch_api(monkeypatch, botmod,
              {"/approvals/apr_exp/approve": {"ok": False, "state": "expired"}})
    u = fake_update("dgt:apr:apr_exp:ok")
    await botmod.callback_dirigent(u, None)
    q = u.callback_query
    assert q.answers[0]["alert"] is True
    assert "истёк" in q.answers[0]["text"]
    assert "повтор не применится" in q.answers[0]["text"]
    assert q.markup_edits == [None]          # кнопки сняты: апрув мёртв


async def test_callback_missing_keeps_buttons(botmod, monkeypatch):
    """Не найдено/ошибка — карточку не причёсываем: причина может быть временной."""
    patch_api(monkeypatch, botmod,
              {"/approvals/apr_x/approve": {"ok": False, "state": "missing"}})
    u = fake_update("dgt:apr:apr_x:ok")
    await botmod.callback_dirigent(u, None)
    assert u.callback_query.markup_edits == []


async def test_callback_second_click_reports_already_decided(botmod, monkeypatch):
    patch_api(monkeypatch, botmod,
              {"/approvals/apr_x/approve": {"ok": False, "state": "approved"}})
    u = fake_update("dgt:apr:apr_x:ok")
    await botmod.callback_dirigent(u, None)
    assert u.callback_query.answers[0]["alert"] is True
    assert "Уже разрешён" in u.callback_query.answers[0]["text"]


async def test_callback_api_offline_acks_with_error(botmod, monkeypatch):
    def boom(path):
        raise OSError("api down")
    monkeypatch.setattr(botmod, "_post_json", boom)
    u = fake_update("dgt:apr:apr_x:ok")
    await botmod.callback_dirigent(u, None)
    q = u.callback_query
    assert q.answers and q.answers[0]["alert"] is True     # ack состоялся
    assert "оффлайн" in q.answers[0]["text"]


async def test_callback_garbage_still_acked(botmod):
    u = fake_update("dgt:apr::ok")
    await botmod.callback_dirigent(u, None)
    assert u.callback_query.answers and u.callback_query.answers[0]["alert"] is True


async def test_callback_ack_retried_if_first_answer_fails(botmod, monkeypatch):
    """Первый answer мог потеряться (таймаут локального Bot API) — добиваем."""
    patch_api(monkeypatch, botmod,
              {"/approvals/apr_x/approve": {"ok": True, "state": "approved"}})
    u = fake_update("dgt:apr:apr_x:ok")
    q = u.callback_query
    original = q.answer
    state = {"n": 0}

    async def flaky(text="", show_alert=False):
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("Bad Gateway (local bot api)")
        await original(text, show_alert)

    q.answer = flaky
    await botmod.callback_dirigent(u, None)
    assert state["n"] >= 2 and q.answers and q.answers[0]["alert"] is False


# ── права пользователя ───────────────────────────────────────
async def test_non_admin_callback_is_acked_and_not_applied(botmod, monkeypatch):
    calls = patch_api(monkeypatch, botmod, {})
    u = fake_update("dgt:apr:apr_x:ok", uid=300241578)     # role=user в allowed_users
    await botmod.callback_dirigent(u, None)
    assert calls == []                                     # API не тронут
    q = u.callback_query
    assert q.answers and "Admin" in q.answers[0]["text"]   # «часики» погашены


async def test_admin_check_uses_user_id_not_username(botmod):
    u = fake_update("dgt:apr:apr_x:ok", uid=859187616)     # Denis, role=user
    assert botmod.is_admin(u) is False
    u2 = fake_update("dgt:apr:apr_x:ok", uid=424106681)    # владелец
    assert botmod.is_admin(u2) is True


def test_admin_check_falls_back_to_allowed_users_file(botmod, monkeypatch, tmp_path):
    """Даже без bot.is_admin права читаются из allowed_users.json по user ID."""
    users = tmp_path / "allowed_users.json"
    users.write_text(json.dumps({"424106681": {"role": "admin"},
                                 "8151894147": {"roles": ["tmbackup"]}}), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "bot", None)          # import bot упадёт
    monkeypatch.setattr(botmod, "_ALLOWED_USERS_FILE", str(users), raising=False)
    assert botmod.is_admin(fake_update("dgt:apr:a:ok", uid=424106681)) is True
    assert botmod.is_admin(fake_update("dgt:apr:a:ok", uid=8151894147)) is False


# ── /approve, /reject команды ────────────────────────────────
async def test_approve_command_reports_expired(botmod, monkeypatch):
    patch_api(monkeypatch, botmod,
              {"/approvals/apr_x/approve": {"ok": False, "state": "timeout"}})
    msg = FakeMessage("/approve apr_x")
    u = types.SimpleNamespace(message=msg, effective_user=types.SimpleNamespace(id=424106681),
                              callback_query=None)
    await botmod.cmd_approve(u, None)
    assert msg.sent and "истёк" in msg.sent[0]["text"]


async def test_outcome_text_mappings(botmod):
    assert botmod.outcome_text({"ok": True, "state": "approved"}, "ok")[1] is False
    for st in ("expired", "timeout", "missing", "approved", "rejected", "weird"):
        text, alert = botmod.outcome_text({"ok": False, "state": st}, "ok")
        assert alert is True and text


# ── роутинг бота: общий callback_handler не перехватывает dgt ──
def test_bot_routing_excludes_dirigent_callbacks():
    src = (BOT_DIR / "bot.py").read_text(encoding="utf-8")
    assert 'CallbackQueryHandler(callback_handler, pattern=r"^(?!dgt[:|])")' in src
    bd = (BOT_DIR / "bot_dirigent.py").read_text(encoding="utf-8")
    assert 'CallbackQueryHandler(callback_dirigent, pattern=r"^dgt[:|]")' in bd
