"""P3a-3: контракт внешнего доступа к /ui/ (без публикации ingress).

Тесты не поднимают туннель и не трогают production. Они фиксируют:
1) текущую карту доступа (только loopback-API + cloudflared на MCP-шлюзе);
2) что API сам не аутентифицирует запросы — значит публикация наружу
   допустима ТОЛЬКО за аутентифицирующим reverse proxy/tunnel;
3) безопасный профиль приложения для такого прокси: /ui/ и read-only GET
   доступны, мутирующие эндпоинты и SSE за прокси запрещены (403).
"""
from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.api import build_app

PUBLIC_PREFIX = "/ui/"


@pytest.fixture
async def client(db):
    app = build_app(db)
    async with TestClient(TestServer(app)) as cli:
        yield cli


@pytest.fixture
async def read_only_client(db):
    """Профиль для публикации за прокси: только GET, без мутаций и SSE."""
    app = build_app(db, public_ui_only=True)
    async with TestClient(TestServer(app)) as cli:
        yield cli


# ── 1. текущая карта доступа ────────────────────────────────

async def test_app_has_no_http_auth(client):
    """API полагается на loopback; сам он запросы не аутентифицирует."""
    r = await client.get("/orders")
    assert r.status == 200                      # без авторизации — норма для 127.0.0.1
    r2 = await client.post("/orders", json={})
    assert r2.status != 401                     # нет 401 от отсутствия креденшелов
    # Пустое тело -> 400 от валидации модели, т.е. эндпоинт достижим анонимно.
    assert r2.status in (400, 403)


async def test_plain_app_exposes_mutations_to_any_client(client):
    """Доказательство необходимости прокси с auth: в обычном профиле POST открыт
    (403 приходит из бизнес-логики, а не из слоя аутентификации)."""
    r = await client.post("/orders", json={"order": {"repo": "x/y"}})
    assert r.status in (400, 403)               # эндпоинт достижим анонимно
    assert "WWW-Authenticate" not in r.headers


# ── 2. безопасный профиль для публикации ────────────────────

async def test_public_profile_serves_ui_index(read_only_client):
    r = await read_only_client.get(f"{PUBLIC_PREFIX}")
    assert r.status == 200
    assert "text/html" in r.headers["Content-Type"]
    assert 'lang="ru"' in await r.text()


async def test_public_profile_serves_static_assets(read_only_client):
    for name in ("app.js", "app.css"):
        r = await read_only_client.get(f"{PUBLIC_PREFIX}{name}")
        assert r.status == 200, name


async def test_public_profile_allows_readonly_get(read_only_client):
    r = await read_only_client.get("/orders")
    assert r.status == 200
    r2 = await read_only_client.get("/stats")
    assert r2.status == 200


async def test_public_profile_blocks_mutations(read_only_client):
    for method, path in (
        ("POST", "/orders"),
        ("POST", "/orders/ord_x/steps/a/retry"),
        ("POST", "/orders/ord_x/pause"),
        ("POST", "/approvals/ap_x/approve"),
        ("POST", "/approvals/ap_x/reject"),
        ("POST", "/orders/ord_x/cancel"),
    ):
        r = await read_only_client.request(method, path, json={})
        assert r.status == 403, f"{method} {path} -> {r.status}"
        assert (await r.json())["error"] == "public_ui_read_only"


async def test_public_profile_blocks_sse(read_only_client):
    r = await read_only_client.get("/api/v1/events")
    assert r.status == 403


async def test_public_profile_head_is_allowed(read_only_client):
    r = await read_only_client.head(f"{PUBLIC_PREFIX}")
    assert r.status == 200


async def test_public_profile_redirects_root_to_ui(read_only_client):
    r = await read_only_client.get("/", allow_redirects=False)
    assert r.status in (301, 302, 303, 307, 308)
    assert r.headers["Location"].rstrip("/").endswith("/ui")


# ── 3. CLI: флаг --public-ui-only включает read-only профиль ─────────────

def test_cli_parser_public_ui_flag_default_off():
    """Без флага `api` остаётся боевым профилем (public_ui_only=False)."""
    from src import cli

    args = cli.build_parser().parse_args(["api"])
    assert args.public_ui_only is False
    assert args.host == "127.0.0.1"
    assert args.port == 9310


def test_cli_parser_public_ui_flag_on():
    """`api --public-ui-only --port 9311` парсится и не слушает наружу."""
    from src import cli

    args = cli.build_parser().parse_args(
        ["api", "--public-ui-only", "--port", "9311"])
    assert args.public_ui_only is True
    assert args.host == "127.0.0.1"
    assert args.port == 9311


async def test_cmd_api_propagates_public_ui_only(monkeypatch):
    """cmd_api пробрасывает public_ui_only в serve() — только loopback."""
    from src import cli
    import src.api as api_mod

    captured: dict = {}

    async def fake_serve(*, host, port, db_path, public_ui_only=False):
        captured.update(host=host, port=port, public_ui_only=public_ui_only)

    monkeypatch.setattr(api_mod, "serve", fake_serve)
    args = cli.build_parser().parse_args(
        ["api", "--public-ui-only", "--port", "9311"])
    await cli.cmd_api(args)
    assert captured == {"host": "127.0.0.1", "port": 9311,
                        "public_ui_only": True}
