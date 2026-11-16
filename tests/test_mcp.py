"""Тесты MCP-шлюза: верификатор токена, песочница, Guard rate-limit."""
import asyncio
import os

import pytest

os.environ.setdefault("DIRIGENT_MCP_TOKEN", "test-tok")


def _load():
    import importlib
    import src.mcp_server as m
    return importlib.reload(m)


def test_verifier_ok_and_bad():
    m = _load()
    v = m.StaticTokenVerifier({"good"})
    assert asyncio.run(v.verify_token("good")) is not None
    assert asyncio.run(v.verify_token("bad")) is None


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
    r = asyncio.run(m.create_test_order(goal="g", request_id="  "))
    assert "error" in r


def test_guard_rate_limit():
    import time
    m = _load()
    guard = m.Guard(lambda s, r, w: asyncio.sleep(0))
    guard.RPM_LIMIT = 3
    now = time.time()
    guard.hits["t"] = [now - 1, now - 2, now - 3]  # три hits в текущем окне
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
