"""MCP-шлюз ChatGPT → Дирижёр (P0-тест связи).

Отдельный тонкий адаптер (спека P0): только 3 безопасных инструмента.
Основной API (127.0.0.1:9310) для внешней сети закрыт — шлюз сам его дёргает
локально. Auth: статический bearer-токен (DIRIGENT_MCP_TOKEN). Журнал вызовов
без секретов, rate-limit, таймауты. create_test_order живёт в отдельной
песочнице (mcp_sandbox.db) — никогда не видна планировщику.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
import uuid

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

API = os.environ.get("DIRIGENT_API", "http://127.0.0.1:9310")
SANDBOX_DB = os.environ.get("DIRIGENT_SANDBOX_DB",
                            "/matrix/dirigent/mcp_sandbox.db")
JOURNAL_DB = os.environ.get("DIRIGENT_DB", "/matrix/dirigent/dirigent.db")
VERSION = "0.1.0-p0"
TIMEOUT = 8.0
RPM_LIMIT = 30          # запросов в минуту на токен


class StaticTokenVerifier(TokenVerifier):
    """Bearer-токен из окружения. Не подходящий -> None -> 401."""

    def __init__(self, tokens: set[str]):
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        if token in self.tokens:
            return AccessToken(token=token, client_id="owner",
                               scopes=["dirigent:read", "dirigent:test-write"])
        return None


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
        pass  # журнал не должен ломать вызов


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
        issuer_url="https://dirigent-mcp.local",
        resource_server_url="https://dirigent-mcp.local",
        required_scopes=[],
        # Аудиторию проверяет сам StaticTokenVerifier; иначе SDK отвергает
        # чужой Host (421) — а у туннеля адрес эфемерный.
        validate_token_resource=False,
    ),
    token_verifier=StaticTokenVerifier(
        {os.environ["DIRIGENT_MCP_TOKEN"]} if os.environ.get("DIRIGENT_MCP_TOKEN") else set()),
)


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
    """ASGI-обёртка: rate-limit на токен + 429. Журнал пишут сами инструменты."""

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
            body = json.dumps({"error": "rate_limited", "rpm": self.RPM_LIMIT}).encode()
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        win.append(now)
        self.hits[token] = win
        await self.app(scope, receive, send)


# DNS-rebind защита MCP SDK по умолчанию режет любой Host кроме localhost:* —
# а у Quick Tunnel адрес эфемерный. Отключаем: сервер и так loopback-only,
# перед ним Cloudflare, плюс обязательный bearer-токен. Для постоянного named
# tunnel вернём allowed_hosts с фиксированным доменом.
app = Guard(server.streamable_http_app(
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False),
))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9320, log_level="warning")
