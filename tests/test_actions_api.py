"""Тесты actions (апрувы/пауза/статистика) и HTTP API."""
import json

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from src import actions
from src.api import build_app
from src.db import DB
from src.models import OrderSpec
from src.states import OrderState, StepState
from tests.conftest import run_ticks, yaml_order


async def submit(db: DB, steps, goal="t") -> str:
    spec = OrderSpec.model_validate(yaml.safe_load(yaml_order(steps, goal=goal)))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


async def test_approve_after_via_actions(db, sched):
    oid = await submit(db, [
        {"id": "s1", "type": "mock.ok",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 60}}])
    await run_ticks(sched, 5)
    cur = await db.conn.execute(
        "SELECT id FROM approvals WHERE order_id=? AND status='open'", (oid,))
    aid = (await cur.fetchone())["id"]
    assert await actions.approve(db, aid)
    st = {r["id"]: StepState(r["state"]) for r in await db.steps_of(oid)}
    assert st["s1"] == StepState.completed


async def test_reject_via_actions(db, sched):
    oid = await submit(db, [
        {"id": "s1", "type": "mock.ok",
         "approval": {"mode": "after", "default_action": "wait", "timeout_seconds": 60}}])
    await run_ticks(sched, 5)
    cur = await db.conn.execute(
        "SELECT id FROM approvals WHERE order_id=? AND status='open'", (oid,))
    aid = (await cur.fetchone())["id"]
    assert await actions.reject(db, aid)
    st = {r["id"]: StepState(r["state"]) for r in await db.steps_of(oid)}
    assert st["s1"] == StepState.cancelled


async def test_pause_blocks_launch(db, sched):
    oid = await submit(db, [{"id": "a", "type": "mock.ok 5"}])
    await db.promote_pending()
    assert await actions.pause_order(db, oid)
    # ready_steps не отдаёт шаги паузнутого заказа
    assert await db.ready_steps(limit=5) == []
    assert await actions.resume_order(db, oid)
    assert len(await db.ready_steps(limit=5)) == 1


async def test_stats_honest_unknown(db, sched):
    s = await actions.stats(db)
    assert s["spent_usd"] == "unknown"   # нет данных — честный unknown
    assert s["codex_quota"] == "unknown"
    oid = await submit(db, [{"id": "a", "type": "mock.ok"}])
    await run_ticks(sched, 5)
    s2 = await actions.stats(db)
    assert s2["orders_done"] == 1


# ── HTTP API ──────────────────────────────────────────────
@pytest.fixture
async def client(db, aiohttp_client):
    app = build_app(db)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    yield cli
    await cli.close()


async def test_api_flow(db, client):
    spec = OrderSpec.model_validate(yaml.safe_load(
        yaml_order([{"id": "a", "type": "mock.ok"}], goal="api-test")))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)

    r = await client.get("/orders")
    assert r.status == 200
    orders = await r.json()
    assert any(o["id"] == oid for o in orders)

    r = await client.get(f"/orders/{oid}")
    detail = await r.json()
    assert detail["goal"] == "api-test"
    assert detail["steps"][0]["id"] == "a"

    r = await client.get("/stats")
    s = await r.json()
    assert "spent_usd" in s

    r = await client.post(f"/orders/{oid}/pause")
    assert (await r.json())["ok"]
    r = await client.post(f"/orders/{oid}/resume")
    assert (await r.json())["ok"]

    r = await client.get(f"/orders/{oid}/log")
    log = await r.json()
    assert any(e["kind"] == "order.created" for e in log)


async def test_api_404(client):
    r = await client.get("/orders/ord_nope")
    assert r.status == 404
