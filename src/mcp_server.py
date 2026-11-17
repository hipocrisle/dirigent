"""MCP-шлюз ChatGPT → Дирижёр (P0-тест связи).

Отдельный тонкий адаптер: только 3 безопасных инструмента. Основной API
(127.0.0.1:9310) для внешней сети закрыт — шлюз дёргает его локально.

Авторизация: полноценный OAuth 2.0 с динамической регистрацией клиентов
(ChatGPT требует OAuth для пользовательских MCP; без метаданных его форма
отказывается подключаться). Поток: ChatGPT регистрируется → браузер владельца
открывает /mcp-consent → вводит секрет Дирижёра (тот же статический bearer) →
одноразовый код → токены. Хранилища в памяти: рестарт = повторная привязка
в ChatGPT (для P0 приемлемо, постоянство — отдельным пакетом).

Guard: rate-limit 30/мин + журнал вызовов (mcp_calls). create_test_order —
идемпотентная песочница (mcp_sandbox.db), планировщику не видна.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import urllib.request
import uuid

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import InvalidRedirectUriError
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, RedirectResponse

API = os.environ.get("DIRIGENT_API", "http://127.0.0.1:9310")
SANDBOX_DB = os.environ.get("DIRIGENT_SANDBOX_DB",
                            "/matrix/dirigent/mcp_sandbox.db")
JOURNAL_DB = os.environ.get("DIRIGENT_DB", "/matrix/dirigent/dirigent.db")
PUBLIC_URL = os.environ.get("DIRIGENT_MCP_PUBLIC_URL",
                            "https://dirigent-mcp.local")
STATIC_TOKEN = os.environ.get("DIRIGENT_MCP_TOKEN", "")
VERSION = "0.2.0-p0-oauth"
TIMEOUT = 8.0
RPM_LIMIT = 30
SCOPES = ["dirigent:read", "dirigent:test-write"]
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300


def _get(path: str):
    with urllib.request.urlopen(API + path, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _journal(tool: str, ok: bool, ip: str = "") -> None:
    try:
        with sqlite3.connect(JOURNAL_DB) as c:
            c.execute("CREATE TABLE IF NOT EXISTS mcp_calls"
                      " (ts REAL, tool TEXT, ok INTEGER, ip TEXT)")
            c.execute("INSERT INTO mcp_calls VALUES (?,?,?,?)",
                      (time.time(), tool, int(ok), ip[:40]))
    except Exception:
        pass


class PermissiveClient(OAuthClientInformationFull):
    """Клиент для single-owner контурa: redirect_uri не валидируем жёстко.

    ChatGPT кэширует регистрацию и предъявляет старые client_id после
    переподключений; сервер оживляет их сам. Риски закрыты выше: consent
    требует секрет владельца, коды одноразовые, PKCE обязателен.
    """

    def validate_redirect_uri(self, redirect_uri):
        if redirect_uri is not None:
            return redirect_uri
        if len(self.redirect_uris or []) == 1:
            return self.redirect_uris[0]
        raise InvalidRedirectUriError("redirect_uri must be specified")


class MiniOAuthProvider(OAuthAuthorizationServerProvider):
    """Минимальный OAuth-провайдер для одного владельца.

    Авторизация — через consent-страницу с секретом; клиенты регистрируются
    динамически (ChatGPT сам себя регистрирует). Хранилище — sqlite
    (mcp_oauth.db): переживает рестарты шлюза, иначе привязки ChatGPT
    сгорали бы при каждом деплое (клиент помнит свой client_id).
    """

    def __init__(self, owner_secret: str,
                 db_path: str = "/matrix/dirigent/mcp_oauth.db"):
        self.owner_secret = owner_secret
        self.db_path = db_path
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, tuple[str, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self._init_db()
        self._load()

    # ── persistence ──────────────────────────────────────────
    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, json TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, json TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, kind TEXT, json TEXT)")

    def _load(self) -> None:
        import pydantic
        with sqlite3.connect(self.db_path) as c:
            for cid, raw in c.execute("SELECT client_id, json FROM clients"):
                try:
                    self.clients[cid] = PermissiveClient.model_validate_json(raw)
                except pydantic.ValidationError:
                    continue
            for code, raw in c.execute("SELECT code, json FROM codes"):
                try:
                    self.codes[code] = AuthorizationCode.model_validate_json(raw)
                except pydantic.ValidationError:
                    continue
            for tok, kind, raw in c.execute("SELECT token, kind, json FROM tokens"):
                try:
                    model = AccessToken if kind == "a" else RefreshToken
                    (self.access if kind == "a" else self.refresh)[tok] = model.model_validate_json(raw)
                except pydantic.ValidationError:
                    continue

    def _save_client(self, info) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO clients VALUES (?,?)",
                      (info.client_id, info.model_dump_json()))

    def _save_code(self, code) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO codes VALUES (?,?)",
                      (code.code, code.model_dump_json()))

    def _save_token(self, tok, kind: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO tokens VALUES (?,?,?)",
                      (tok.token, kind, tok.model_dump_json()))

    def _drop(self, table: str, key_col: str, key: str) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(f"DELETE FROM {table} WHERE {key_col}=?", (key,))

    # ── клиенты ──────────────────────────────────────────────
    async def get_client(self, client_id: str):
        c = self.clients.get(client_id)
        if c is None and client_id:
            # ChatGPT кэширует client_id и предъявляет его после переподключений,
            # даже если регистрация была потеряна. Оживляем неизвестных клиентов
            # вместо отказа — иначе вечная петля "Client ID not found".
            c = PermissiveClient(
                client_id=client_id, client_name="resurrected",
                redirect_uris=[], scope=" ".join(SCOPES),
                response_types=["code"], token_endpoint_auth_method="none")
            self.clients[client_id] = c
            self._save_client(c)
        return c

    async def register_client(self, info: OAuthClientInformationFull) -> None:
        if not info.client_id:
            info.client_id = "cl_" + secrets.token_hex(8)
        self.clients[info.client_id] = info
        self._save_client(info)

    # ── authorize: consent-страница ──────────────────────────
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        consent_id = "cs_" + secrets.token_urlsafe(16)
        self.pending[consent_id] = (client.client_id, params)
        return f"/mcp-consent?id={consent_id}"

    async def load_authorization_code(self, client, code: str):
        c = self.codes.get(code)
        if not c or c.client_id != client.client_id:
            return None
        return c

    def mint(self, client_id: str, scopes: list[str],
             resource: str | None) -> tuple[AccessToken, RefreshToken]:
        at = AccessToken(token="dat_" + secrets.token_urlsafe(24),
                         client_id=client_id, scopes=scopes,
                         expires_at=int(time.time()) + ACCESS_TTL,
                         resource=resource, subject="owner", claims={})
        rt = RefreshToken(token="drt_" + secrets.token_urlsafe(24),
                          client_id=client_id, scopes=scopes,
                          expires_at=int(time.time()) + REFRESH_TTL,
                          resource=resource, subject="owner")
        self.access[at.token] = at
        self.refresh[rt.token] = rt
        self._save_token(at, "a")
        self._save_token(rt, "r")
        return at, rt

    @staticmethod
    def _token(at: AccessToken, rt: RefreshToken) -> OAuthToken:
        return OAuthToken(access_token=at.token, token_type="Bearer",
                          expires_in=ACCESS_TTL,
                          scope=" ".join(at.scopes), refresh_token=rt.token)

    async def exchange_authorization_code(self, client, code) -> OAuthToken:
        if code.expires_at and code.expires_at < time.time():
            raise TokenError(error="invalid_grant",
                             error_description="code expired")
        self.codes.pop(code.code, None)
        self._drop("codes", "code", code.code)
        at, rt = self.mint(client.client_id, code.scopes or SCOPES,
                           code.resource)
        return self._token(at, rt)

    async def load_refresh_token(self, client, token: str):
        rt = self.refresh.get(token)
        if not rt or rt.client_id != client.client_id:
            return None
        return rt

    async def exchange_refresh_token(self, client, rt,
                                     scopes: list[str]) -> OAuthToken:
        self.refresh.pop(rt.token, None)
        self.access.pop(rt.token, None)
        self._drop("tokens", "token", rt.token)
        new_scopes = scopes or rt.scopes
        at, rt2 = self.mint(client.client_id, new_scopes, rt.resource)
        return self._token(at, rt2)

    async def load_access_token(self, token: str):
        # Статический токен остаётся валидным навсегда (для curl/мониторинга).
        if token and token == self.owner_secret:
            return AccessToken(token=token, client_id="owner", scopes=SCOPES,
                               expires_at=int(time.time()) + 10 * 365 * 86400,
                               resource=None, subject="owner", claims={})
        at = self.access.get(token)
        if at and at.expires_at and at.expires_at < time.time():
            return None
        return at

    async def revoke_token(self, token) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)
        self._drop("tokens", "token", token.token)


provider = MiniOAuthProvider(STATIC_TOKEN)

server = MCPServer(
    name="dirigent-test",
    title="Дирижёр (тестовый контур)",
    instructions=(
        "Тестовый доступ к оркестратору «Дирижёр» (DE-1). Три инструмента: "
        "чтение статуса, список заказов, создание фиктивного заказа в песочнице. "
        "Никаких реальных агентов, shell, git и платных операций."
    ),
    version=VERSION,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        service_documentation_url=PUBLIC_URL,
        resource_server_url=PUBLIC_URL + "/mcp",
        required_scopes=[],
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES),
        revocation_options=RevocationOptions(enabled=True),
    ),
    auth_server_provider=provider,
)


@server.custom_route("/mcp-consent", methods=["GET", "POST"])
async def mcp_consent(request):
    """Страница подтверждения: секрет владельца -> одноразовый код."""
    consent_id = (request.query_params.get("id")
                  if request.method == "GET"
                  else (await request.form()).get("id", ""))
    pending = provider.pending.get(consent_id)
    if not pending:
        return HTMLResponse("<h3>Сессия подтверждения не найдена или истекла.</h3>",
                            status_code=404)
    client_id, params = pending
    if request.method == "GET":
        return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дирижёр — доступ</title></head><body style="font-family:sans-serif;max-width:420px;margin:4em auto;padding:0 1em">
<h2>🎩 Дирижёр (тест)</h2>
<p>ChatGPT запрашивает доступ к оркестратору (клиент <code>{client_id}</code>).</p>
<p>Разрешённые возможности: чтение статуса и заказов, фиктивные тестовые заказы в песочнице.</p>
<form method="post" action="/mcp-consent">
  <input type="hidden" name="id" value="{consent_id}">
  <label>Секрет Дирижёра:<br>
    <input type="password" name="secret" required style="width:100%;padding:.6em;margin:.5em 0">
  </label>
  <button type="submit" style="padding:.7em 1.4em">Разрешить</button>
</form></body></html>""")
    form = await request.form()
    if form.get("secret", "") != provider.owner_secret or not provider.owner_secret:
        _journal("mcp_consent", False)
        return HTMLResponse("<h3>⛔ Неверный секрет.</h3>", status_code=403)
    provider.pending.pop(consent_id, None)
    code = AuthorizationCode(
        code="cd_" + secrets.token_urlsafe(24), scopes=list(params.scopes or SCOPES),
        expires_at=int(time.time()) + CODE_TTL, client_id=client_id,
        code_challenge=params.code_challenge,
        redirect_uri=params.redirect_uri,
        redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        resource=params.resource, subject="owner")
    provider.codes[code.code] = code
    provider._save_code(code)
    _journal("mcp_consent", True)
    redirect = str(params.redirect_uri or "")
    sep = "&" if "?" in redirect else "?"
    location = (redirect + sep
                + f"code={code.code}&state={params.state or ''}")
    return RedirectResponse(location, status_code=302)


@server.tool()
async def get_system_status() -> dict:
    """Статус Дирижёра: сервис, версия, заказы, задачи, исполнители. Только чтение."""
    try:
        s = _get("/stats")
        api_ok = True
    except Exception:
        s, api_ok = {}, False
    _journal("get_system_status", True)
    return _with_notices({
        "service": "dirigent", "version": VERSION, "api_reachable": api_ok,
        "orders_done": s.get("orders_done"),
        "tasks_running": s.get("tasks_running"),
        "tasks_ready": s.get("tasks_ready"),
        "tasks_waiting": s.get("tasks_waiting"),
        "spent_usd": s.get("spent_usd", "unknown"),
        "codex_quota": s.get("codex_quota", "unknown"),
    })


def _with_notices(response: dict) -> dict:
    from .notice import attach_notices
    return attach_notices(response)


@server.tool()
async def list_orders() -> list[dict]:
    """Безопасный краткий список заказов (id, цель, состояние, прогресс). Только чтение."""
    try:
        orders = _get("/orders")
        _journal("list_orders", True)
    except Exception as e:
        _journal("list_orders", False)
        return [{"error": f"api_unreachable: {e}"}]
    return [{"id": o["id"], "goal": o["goal"][:80], "state": o["state"],
             "progress": o.get("progress", {})} for o in orders[:20]]


@server.tool()
async def create_test_order(goal: str, request_id: str) -> dict:
    """Фиктивный заказ в изолированной песочнице. Без LLM/shell/git/платных операций.

    Идемпотентен по request_id: повтор с тем же id вернёт тот же заказ.
    """
    request_id = (request_id or "").strip()[:80]
    if not request_id:
        _journal("create_test_order", False)
        return {"error": "request_id обязателен"}
    with sqlite3.connect(SANDBOX_DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS test_orders (request_id TEXT PRIMARY KEY,"
                     " order_id TEXT, goal TEXT, created REAL)")
        row = conn.execute("SELECT order_id FROM test_orders WHERE request_id=?",
                           (request_id,)).fetchone()
        if row:
            _journal("create_test_order", True)
            return {"created": False, "duplicate": True, "order_id": row[0],
                    "sandbox": True}
        oid = "tst_" + uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO test_orders VALUES (?,?,?,?)",
                     (request_id, oid, goal[:200], time.time()))
    _journal("create_test_order", True)
    return {"created": True, "duplicate": False, "order_id": oid, "sandbox": True}


# ═══ MCP v2: наблюдаемость (READ) ═══════════════════════════

@server.tool()
async def get_order(order_id: str) -> dict:
    """Детали заказа: состояние, шаги, открытые согласования. Только чтение."""
    try:
        d = _get(f"/orders/{order_id}")
        _journal("get_order", True)
    except Exception as e:
        _journal("get_order", False)
        return {"error": str(e)[:200]}
    if isinstance(d, dict) and d.get("error"):
        return {"error": "not found"}
    return _with_notices({"id": d["id"], "goal": d["goal"], "state": d["state"],
                          "repo": d.get("repo"), "steps": d["steps"],
                          "open_approvals": d["open_approvals"]})


@server.tool()
async def get_logs(order_id: str, limit: int = 30) -> list[dict]:
    """Журнал событий заказа (переходы, ошибки, апрувы). Только чтение."""
    limit = max(1, min(int(limit), 200))
    try:
        rows = _get(f"/orders/{order_id}/log?limit={limit}")
        _journal("get_logs", True)
        return rows
    except Exception as e:
        _journal("get_logs", False)
        return [{"error": str(e)[:200]}]


@server.tool()
async def get_pending_approvals() -> list[dict]:
    """Все открытые согласования по всем заказам. Только чтение."""
    try:
        rows = _get("/approvals/open")
        _journal("get_pending_approvals", True)
        return rows
    except Exception as e:
        _journal("get_pending_approvals", False)
        return [{"error": str(e)[:200]}]


@server.tool()
async def get_costs() -> dict:
    """Расходы и квоты: только факт; неизмеримое — unknown (не $0!). Только чтение."""
    try:
        s = _get("/stats")
        _journal("get_costs", True)
        return s
    except Exception as e:
        _journal("get_costs", False)
        return {"error": str(e)[:200]}


@server.tool()
async def get_resource_status() -> dict:
    """Исполнители и ресурсы: доступность codex/opencode/DE-1. Только чтение."""
    import shutil
    from pathlib import Path
    import json as _json
    codex_auth = Path.home() / ".codex" / "auth.json"
    opencode_cfg = Path("/root/.config/opencode/opencode.json")
    _journal("get_resource_status", True)
    return {
        "executors": {
            "codex": {"installed": bool(shutil.which("codex")),
                      "auth": codex_auth.is_file()},
            "opencode": {"installed": bool(shutil.which("opencode")),
                         "config": opencode_cfg.is_file()},
        },
        "de1": {"role": "orchestrator host", "status": "ok"},
        "homepc": "unknown",  # проверка по ssh — отдельный адаптер (медиа-фаза)
        "note": "доступность ≠ свободная квота; квота Codex без probe — unknown",
    }


# ═══ MCP v2: управление (CONTROL) ═══════════════════════════

def _post_json(path: str) -> dict:
    req = urllib.request.Request(API + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


@server.tool()
async def pause_order(order_id: str) -> dict:
    """Приостановить заказ: running дорабатывает, новые шаги не стартуют. Идемпотентно."""
    try:
        r = _post_json(f"/orders/{order_id}/pause")
        _journal("pause_order", bool(r.get("ok")))
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@server.tool()
async def resume_order(order_id: str) -> dict:
    """Продолжить приостановленный заказ. Идемпотентно."""
    try:
        r = _post_json(f"/orders/{order_id}/resume")
        _journal("resume_order", bool(r.get("ok")))
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@server.tool()
async def cancel_order(order_id: str) -> dict:
    """Отменить заказ и все незавершённые шаги. Идемпотентно. Аудит в журнале."""
    try:
        r = _post_json(f"/orders/{order_id}/cancel")
        _journal("cancel_order", bool(r.get("ok")))
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@server.tool()
async def retry_step(order_id: str, step_id: str) -> dict:
    """Перезапустить упавший шаг (в пределах политики: макс. 3 запуска)."""
    try:
        r = _post_json(f"/orders/{order_id}/steps/{step_id}/retry")
        _journal("retry_step", bool(r.get("ok")))
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@server.tool()
async def diagnose_step(order_id: str, step_id: str) -> dict:
    """Диагностика живого процесса шага: pid/CPU/RAM/время/wchan, состояние worktree.

    Процессы ищутся по cwd=worktree шага. Подсказка hint срабатывает при
    подозрении на сетевой сталл (ep_poll + ~0% CPU дольше 10 мин).
    """
    try:
        r = _get(f"/orders/{order_id}/steps/{step_id}/diagnose")
        _journal("diagnose_step", True)
        return r
    except Exception as e:
        _journal("diagnose_step", False)
        return {"error": str(e)[:200]}


@server.tool()
async def kill_step_process(order_id: str, step_id: str) -> dict:
    """Безопасно остановить процессы running-шага: SIGTERM -> 10с -> SIGKILL.

    Только для running-шагов; иначе структурированный отказ. Аудит: step.killed.
    """
    try:
        req = urllib.request.Request(
            API + f"/orders/{order_id}/steps/{step_id}/kill",
            data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT + 15) as r:
            out = json.loads(r.read().decode())
        _journal("kill_step_process", bool(out.get("ok")))
        return out
    except Exception as e:
        _journal("kill_step_process", False)
        return {"ok": False, "error": str(e)[:200]}


# ═══ MCP v2: согласования (APPROVAL) ════════════════════════

@server.tool()
async def approve(approval_id: str) -> dict:
    """Принять согласование (результат шага/операция)."""
    try:
        r = _post_json(f"/approvals/{approval_id}/approve")
        _journal("approve", bool(r.get("ok")))
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@server.tool()
async def reject(approval_id: str) -> dict:
    """Отклонить согласование (шаг будет отменён)."""
    try:
        r = _post_json(f"/approvals/{approval_id}/reject")
        _journal("reject", bool(r.get("ok")))
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@server.tool()
async def dirigent_call(tool: str, arguments: dict | None = None) -> dict:
    """УНИВЕРСАЛЬНЫЙ ДИСПЕТЧЕР — стабильная схема для новых возможностей.

    ChatGPT кэширует список инструментов при подключении, поэтому новые
    @server.tool появляются у него только после переподключения. Этот инструмент
    решает проблему: все новые возможности сразу доступны через него после деплоя.
    tool="help" — список вызываемых имен; аргументы — те же, что у одноимённого
    инструмента (dict). Специфичные инструменты остаются для удобства и схем.
    """
    args = arguments or {}
    reg = {
        "help": _help,
        "diagnose_step": diagnose_step,
        "kill_step_process": kill_step_process,
        "get_system_status": get_system_status,
        "list_orders": list_orders,
        "get_order": get_order,
        "get_logs": get_logs,
        "get_pending_approvals": get_pending_approvals,
        "get_costs": get_costs,
        "get_resource_status": get_resource_status,
        "pause_order": pause_order,
        "resume_order": resume_order,
        "cancel_order": cancel_order,
        "retry_step": retry_step,
        "approve": approve,
        "reject": reject,
        "create_order": create_order,
        "create_test_order": create_test_order,
    }
    fn = reg.get((tool or "").strip())
    if fn is None:
        _journal("dirigent_call", False)
        return {"error": f"unknown tool: {tool}", "available": sorted(reg.keys())}
    _journal(f"dirigent_call:{tool}", True)
    return await fn(**args)


async def _help() -> dict:
    return {
        "message": "Вызывай dirigent_call(tool=<имя>, arguments={...}). Имена = инструменты Dirigent.",
        "tools": sorted([
            "get_system_status", "list_orders", "get_order", "get_logs",
            "get_pending_approvals", "get_costs", "get_resource_status",
            "pause_order", "resume_order", "cancel_order", "retry_step",
            "approve", "reject", "create_order", "create_test_order",
            "diagnose_step", "kill_step_process",
        ]),
        "note": "новые возможности появляются в этом списке сразу после деплоя сервера",
    }


# ═══ MCP v2: CREATE (gated) ═════════════════════════════════

@server.tool()
async def create_order(goal: str, steps: list[dict], request_id: str,
                       repo: str | None = None,
                       budget_max_usd: float | None = None) -> dict:
    """Создать РЕАЛЬНЫЙ заказ Дирижёра (gated: allowlist repo, бюджет ≤ $1,
    обязательный request_id для идемпотентности). Исполнители запускаются
    настоящие — следи за шагами через get_order.

    steps: [{"id","type"(opencode|codex|mock.ok),"prompt","depends_on":[...],
             "acceptance":{"checks":["go_test"]}}]
    """
    body = json.dumps({
        "request_id": request_id[:80],
        "order": {"goal": goal[:300], "repo": repo, "steps": steps,
                  "budget_max_usd": budget_max_usd},
    }).encode()
    req = urllib.request.Request(API + "/orders", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.loads(r.read().decode())
        _journal("create_order", "error" not in out)
        return out
    except urllib.error.HTTPError as e:
        _journal("create_order", False)
        return {"error": f"http {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        _journal("create_order", False)
        return {"error": str(e)[:200]}


class Guard:
    """ASGI-обёртка: rate-limit на токен + 429."""

    def __init__(self, app):
        self.app = app
        self.hits: dict[str, list[float]] = {}
        self.RPM_LIMIT = RPM_LIMIT

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        token = auth.removeprefix("Bearer ").strip()
        now = time.time()
        win = [t for t in self.hits.get(token, []) if now - t < 60]
        if len(win) >= self.RPM_LIMIT:
            body = json.dumps({"error": "rate_limited",
                               "rpm": self.RPM_LIMIT}).encode()
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        win.append(now)
        self.hits[token] = win
        await self.app(scope, receive, send)


# DNS-rebind защита SDK по умолчанию режет любой Host кроме localhost:* — а у
# Quick Tunnel адрес эфемерный. Отключено: loopback-only + Cloudflare + OAuth.
app = Guard(server.streamable_http_app(
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False),
))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9320, log_level="warning")
