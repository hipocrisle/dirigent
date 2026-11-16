"""Тесты MCP-шлюза: OAuth-провайдер, песочница, Guard rate-limit."""
import asyncio
import base64
import hashlib
import os
import secrets

import pytest

os.environ.setdefault("DIRIGENT_MCP_TOKEN", "test-owner-secret")


def _load():
    import importlib
    import src.mcp_server as m
    return importlib.reload(m)


def test_provider_full_oauth_flow():
    m = _load()
    p = m.provider
    run = asyncio.run

    async def flow():
        # регистрация клиента
        info = m.OAuthClientInformationFull(
            client_id="cl_test", client_name="t",
            redirect_uris=["https://cb.example/x"],
            response_types=["code"], token_endpoint_auth_method="none")
        await p.register_client(info)
        assert await p.get_client(info.client_id)
        # authorize -> consent id
        params = m.AuthorizationParams(
            state="s1", scopes=["dirigent:read"],
            redirect_uri="https://cb.example/x",
            redirect_uri_provided_explicitly=True,
            code_challenge="ch", resource=None)
        loc = await p.authorize(info, params)
        assert loc.startswith("/mcp-consent?id=")
        csid = loc.split("id=")[1]
        # имитация consent POST: мини-логика из хендлера
        client_id, prm = p.pending.pop(csid)
        assert client_id == info.client_id
        code = m.AuthorizationCode(
            code="cd_test", scopes=list(prm.scopes), expires_at=int(asyncio.get_event_loop().time()) + 300,
            client_id=client_id, code_challenge=prm.code_challenge,
            redirect_uri=str(prm.redirect_uri),
            redirect_uri_provided_explicitly=prm.redirect_uri_provided_explicitly,
            resource=None, subject="owner")
        p.codes[code.code] = code
        # exchange
        tok = await p.exchange_authorization_code(info, code)
        assert tok.access_token.startswith("dat_")
        # код одноразовый
        assert await p.load_authorization_code(info, "cd_test") is None
        # доступ по токену
        assert await p.load_access_token(tok.access_token) is not None
        # статический токен владельца тоже валиден
        assert await p.load_access_token("test-owner-secret") is not None
        # чужой токен — нет
        assert await p.load_access_token("nope") is None
        # revoke
        at = await p.load_access_token(tok.access_token)
        await p.revoke_token(at)
        assert await p.load_access_token(tok.access_token) is None
        return True
    assert run(flow())


def test_pkce_helper_shapes():
    v = secrets.token_urlsafe(32)
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert len(ch) == 43 and "=" not in ch


def test_sandbox_idempotent(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "SANDBOX_DB", str(tmp_path / "sb.db"))
    r1 = asyncio.run(m.create_test_order(goal="g", request_id="r1"))
    r2 = asyncio.run(m.create_test_order(goal="g", request_id="r1"))
    assert r1["created"] and not r1["duplicate"]
    assert r2["duplicate"] and r2["order_id"] == r1["order_id"]


def test_sandbox_requires_request_id(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "SANDBOX_DB", str(tmp_path / "sb.db"))
    assert "error" in asyncio.run(m.create_test_order(goal="g", request_id="  "))


def test_guard_rate_limit():
    import time
    m = _load()
    guard = m.Guard(lambda s, r, w: asyncio.sleep(0))
    guard.RPM_LIMIT = 3
    now = time.time()
    guard.hits["t"] = [now - 1, now - 2, now - 3]
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer t")]}
    out = []
    async def send(msg):
        out.append(msg)
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    asyncio.run(guard(scope, receive, send))
    assert out and out[0]["status"] == 429


def test_app_builds():
    m = _load()
    assert m.app is not None
