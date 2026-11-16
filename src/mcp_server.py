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


class MiniOAuthProvider(OAuthAuthorizationServerProvider):
    """Минимальный OAuth-провайдер для одного владельца.

    Авторизация — через consent-страницу с секретом; клиенты регистрируются
    динамически (ChatGPT сам себя регистрирует). Всё в памяти.
    """

    def __init__(self, owner_secret: str):
        self.owner_secret = owner_secret
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, tuple[str, AuthorizationParams]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}

    # ── клиенты ──────────────────────────────────────────────
    async def get_client(self, client_id: str):
        return self.clients.get(client_id)

    async def register_client(self, info: OAuthClientInformationFull) -> None:
        if not info.client_id:
            info.client_id = "cl_" + secrets.token_hex(8)
        self.clients[info.client_id] = info

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
    return {
        "service": "dirigent", "version": VERSION, "api_reachable": api_ok,
        "orders_done": s.get("orders_done"),
        "tasks_running": s.get("tasks_running"),
        "tasks_ready": s.get("tasks_ready"),
        "tasks_waiting": s.get("tasks_waiting"),
        "spent_usd": s.get("spent_usd", "unknown"),
        "codex_quota": s.get("codex_quota", "unknown"),
    }


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
