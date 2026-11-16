from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict, MultiDict

from src.api import build_app
from src.db import DB
from src.event_stream import (PAGE_LIMIT, EventStream, format_event,
                              parse_cursor, public_event)
from src.models import OrderSpec
from src.states import OrderState
from tests.conftest import yaml_order

TIMEOUT = 5.0


class SseReader:
    """Инкрементальное чтение SSE: события (id/event/data), комментарии, retry."""

    def __init__(self, resp):
        self.resp = resp
        self.events: list[dict] = []
        self.comments: list[str] = []
        self.retry: list[int] = []

    async def read_more(self, want_events: int = 0, want_comments: int = 0,
                        timeout: float = TIMEOUT) -> None:
        async def _loop():
            while len(self.events) < want_events or len(self.comments) < want_comments:
                raw = await self.resp.content.readline()
                line = raw.decode().rstrip("\r\n")
                if not line:
                    continue
                if line.startswith(":"):
                    self.comments.append(line[1:].strip())
                elif line.startswith("retry:"):
                    self.retry.append(int(line[6:].strip()))
                elif line.startswith("data:"):
                    self.events.append(json.loads(line[5:].strip()))
        await asyncio.wait_for(_loop(), timeout)

    async def read_n(self, n: int, timeout: float = TIMEOUT, start: int = 0) -> list:
        """Дождаться n новых событий (после уже прочитанных) и вернуть только новые."""
        await self.read_more(want_events=start + n, timeout=timeout)
        return self.events[start:]

    async def read_heartbeat(self, timeout: float = TIMEOUT) -> None:
        await self.read_more(want_comments=len(self.comments) + 1, timeout=timeout)

    async def wait_closed(self, timeout: float = TIMEOUT) -> None:
        async def _drain():
            while await self.resp.content.readline():
                pass
        await asyncio.wait_for(_drain(), timeout)


# ── фикстуры ──────────────────────────────────────────────
def fast_stream(db: DB, **kw) -> EventStream:
    opts = {"heartbeat": 0.1, "stream_ttl": 60, "min_poll": 0.01, "idle_poll": 0.02}
    opts.update(kw)
    return EventStream(db, **opts)


async def _server(app):
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def srv(db):
    """Сервер с короткими лимитами: heartbeat раз в 100мс, быстрый опрос."""
    server = await _server(build_app(db, event_stream=fast_stream(db)))
    yield server
    await server.close()


@pytest.fixture
async def srv_ttl(db):
    """Сервер, который штатно закрывает поток через 0.3с (проверка reconnect)."""
    server = await _server(build_app(
        db, event_stream=fast_stream(db, heartbeat=5, stream_ttl=0.3)))
    yield server
    await server.close()


@pytest.fixture
async def srv_slow(db):
    """Сервер с дефолтным анти-busy-loop поведением (min_poll=0.1с, idle=1с)."""
    server = await _server(build_app(db, event_stream=EventStream(
        db, heartbeat=0.3, stream_ttl=60)))
    yield server
    await server.close()


async def _client(server) -> TestClient:
    cli = TestClient(server)
    await cli.start_server()
    return cli


@pytest.fixture
async def live(srv):
    cli = await _client(srv)
    yield cli
    await cli.close()


@pytest.fixture
async def live_ttl(srv_ttl):
    cli = await _client(srv_ttl)
    yield cli
    await cli.close()


@pytest.fixture
async def live_slow(srv_slow):
    cli = await _client(srv_slow)
    yield cli
    await cli.close()


async def open_stream(cli, params: dict | None = None, headers: dict | None = None):
    """SSE через настоящий HTTP-клиент: соединение читается инкрементально."""
    from aiohttp.client_proto import ResponseHandler
    # Плагин aiohttp-фикстур подменяет протокол и рвёт соединение без Content-Length:
    # для SSE нужен штатный протокол — живёт, пока сервер не закроет соединение.
    cli.session._connector._factory = lambda: ResponseHandler(
        loop=asyncio.get_running_loop())
    # По умолчанию в клиенте total=300с: для долгого потока нужен таймаут без total.
    resp = await cli.session.request(
        "GET", cli.make_url("/api/v1/events"), params=params or {},
        headers=headers or {},
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=None))
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Accel-Buffering"] == "no"
    assert resp.connection is not None and not resp.connection.closed
    return resp, SseReader(resp)


# ── данные ────────────────────────────────────────────────
async def submit(db: DB, steps, goal="sse") -> str:
    spec = OrderSpec.model_validate(yaml.safe_load(yaml_order(steps, goal=goal)))
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    return oid


async def write_events(db: DB, n: int, oid: str = "ord_test",
                       sid: str | None = "s1") -> list[int]:
    """Синтетические события в существующую таблицу events. Возвращает их seq."""
    ids = []
    for i in range(n):
        await db.event("test.event", oid, sid, {"i": i})
        cur = await db.conn.execute("SELECT max(seq) AS s FROM events")
        ids.append((await cur.fetchone())["s"])
    return ids


# ── чистая логика ─────────────────────────────────────────
def _req(query=None, headers=None):
    class _R:
        pass
    r = _R()
    r.query = MultiDict(query or {})
    r.headers = CIMultiDict(headers or {})
    return r


def test_parse_cursor_validation():
    assert parse_cursor(_req()) == (0, None, PAGE_LIMIT, None)
    assert parse_cursor(_req({"last_event_id": "12"}))[:2] == (12, None)
    assert parse_cursor(_req(headers={"Last-Event-ID": "7"}))[0] == 7
    # пустой Last-Event-ID — не ошибка, просто старт с начала
    assert parse_cursor(_req(headers={"Last-Event-ID": ""}))[0] == 0
    # заголовок и query расходятся — берём более новый курсор
    assert parse_cursor(_req({"last_event_id": "3"}, {"Last-Event-ID": "9"}))[0] == 9
    assert parse_cursor(_req({"last_event_id": "9"}, {"Last-Event-ID": "3"}))[0] == 9
    assert parse_cursor(_req({"last_event_id": "abc"}))[3]
    assert parse_cursor(_req({"last_event_id": "-4"}))[3]
    assert parse_cursor(_req({"limit": "0"}))[3]
    assert parse_cursor(_req({"limit": "xyz"}))[3]
    assert parse_cursor(_req({"limit": "100000"}))[2] == PAGE_LIMIT  # жёсткий потолок
    assert parse_cursor(_req({"limit": "5"}))[2] == 5
    assert parse_cursor(_req({"order_id": "x" * 200}))[3]
    assert parse_cursor(_req({"order_id": ""}))[3]
    assert parse_cursor(_req({"order_id": " ord_1 "}))[1] == "ord_1"


def test_public_event_and_format():
    row = {"seq": 5, "ts": 1.5, "order_id": "o", "step_id": "s",
           "kind": "step.transition", "data_json": '{"to": "ready"}'}
    ev = public_event(row)
    assert ev == {"id": 5, "type": "step.transition", "ts": 1.5,
                  "order_id": "o", "step_id": "s", "data": {"to": "ready"}}
    assert public_event({**row, "data_json": None})["data"] == {}
    assert public_event({**row, "data_json": "not json"})["data"] == {}

    raw = format_event(ev)
    text = raw.decode()
    assert text.startswith("id: 5\nevent: step.transition\ndata: {")
    assert text.endswith("\n\n")
    assert '"step_id":"s"' in text


# ── живой поток ───────────────────────────────────────────
async def test_stream_start_headers_and_backlog(db, live):
    oid = await submit(db, [{"id": "s1", "type": "mock.ok"}])
    resp, reader = await open_stream(live)
    try:
        await reader.read_more(want_events=2, want_comments=1)
        first, second = reader.events[:2]
        assert first["type"] == "order.created" and first["data"]["goal"] == "sse"
        assert second["type"] == "order.transition"
        assert first["order_id"] == oid and second["order_id"] == oid
        # контракт полей: стабильный ID, тип, ts, order/step, данные
        for ev in (first, second):
            assert set(ev) == {"id", "type", "ts", "order_id", "step_id", "data"}
            assert isinstance(ev["id"], int) and isinstance(ev["ts"], float)
        assert first["step_id"] is None
        assert first["id"] < second["id"]
        assert reader.comments[0] == "connected id=0"
        assert reader.retry == [2000]
        # snapshot для панели — существующий REST endpoint, второй источник не нужен
        r = await live.get(f"/orders/{oid}")
        assert (await r.json())["id"] == oid
    finally:
        resp.close()


async def test_new_events_after_connect(db, live):
    oid = await submit(db, [{"id": "s1", "type": "mock.ok"}])
    cur = await db.conn.execute("SELECT COALESCE(MAX(seq),0) m FROM events")
    cursor = (await cur.fetchone())["m"]
    resp, reader = await open_stream(live, {"last_event_id": str(cursor)})
    try:
        await reader.read_more(want_comments=1)
        assert reader.events == []          # старые не переотправляются
        await db.event("step.lease", oid, "s1", {"owner": "w1"})
        await db.event("order.paused", oid, data={})
        got = await reader.read_n(2)
        assert [e["type"] for e in got] == ["step.lease", "order.paused"]
        assert got[0]["step_id"] == "s1" and got[0]["data"] == {"owner": "w1"}
        assert got[1]["step_id"] is None
        assert got[0]["id"] < got[1]["id"]  # порядок по seq
    finally:
        resp.close()


async def test_last_event_id_resume_no_gap_no_dup(db, live):
    ids = await write_events(db, 5)
    resp, reader = await open_stream(live, headers={"Last-Event-ID": str(ids[1])})
    try:
        got = await reader.read_n(3)
        assert [e["id"] for e in got] == ids[2:]
    finally:
        resp.close()
    # reconnect с последнего полученного: ни дублей, ни пропусков
    resp, reader = await open_stream(live, {"last_event_id": str(ids[-1])})
    try:
        await reader.read_more(want_comments=1)
        assert reader.events == []
        new_ids = await write_events(db, 2, oid="ord_other", sid=None)
        got = await reader.read_n(2)
        assert [e["id"] for e in got] == new_ids
        assert got[0]["step_id"] is None and got[1]["order_id"] == "ord_other"
    finally:
        resp.close()


async def test_order_is_strictly_increasing_across_pages(db, live):
    """Больше одной страницы: пагинация курсором, без повторов и перестановок."""
    ids = await write_events(db, 25)
    resp, reader = await open_stream(live, {"limit": "7"})
    try:
        got = await reader.read_n(len(ids))
        assert [e["id"] for e in got] == ids
    finally:
        resp.close()


async def test_heartbeat_comment_when_idle(db, live):
    await write_events(db, 1)
    resp, reader = await open_stream(live, {"last_event_id": "1"})
    try:
        await reader.read_heartbeat()
        await reader.read_heartbeat()
        assert reader.events == []
        assert all(c == "heartbeat" for c in reader.comments[1:])
    finally:
        resp.close()


async def test_server_close_then_reconnect_without_loss(db, live_ttl):
    await write_events(db, 2)
    resp, reader = await open_stream(live_ttl, {"last_event_id": "0"})
    got_first = await reader.read_n(2)
    await reader.wait_closed()  # сервер штатно закрыл по TTL
    cursor = got_first[-1]["id"]
    await write_events(db, 2, oid="ord_after")
    resp, reader = await open_stream(live_ttl, {"last_event_id": str(cursor)})
    try:
        got = await reader.read_n(2)  # события, записанные во время даунтайма
        assert [e["id"] for e in got] == [cursor + 1, cursor + 2]
        assert {e["order_id"] for e in got} == {"ord_after"}
    finally:
        resp.close()


async def test_client_abort_then_reconnect_no_loss(db, live):
    """Клиент оборвал соединение (не дождавшись): reconnect доставит всё."""
    ids = await write_events(db, 3)
    resp, reader = await open_stream(live, {"last_event_id": str(ids[0])})
    await reader.read_n(1)
    resp.close()
    await write_events(db, 2)
    resp, reader = await open_stream(live, {"last_event_id": str(ids[0])})
    try:
        got = await reader.read_n(4)
        assert [e["id"] for e in got] == [ids[1], ids[2], ids[3], ids[4]]
    finally:
        resp.close()


async def test_order_filter(db, live):
    await write_events(db, 2, oid="ord_a")
    await write_events(db, 1, oid="ord_b")
    resp, reader = await open_stream(live, {"order_id": "ord_a"})
    try:
        await reader.read_more(want_events=2)
        await write_events(db, 1, oid="ord_b")
        await asyncio.sleep(0.15)  # чужое событие не должно прийти
        assert {e["order_id"] for e in reader.events} == {"ord_a"}
    finally:
        resp.close()


async def test_bad_input_400(live):
    r = await live.get("/api/v1/events", params={"last_event_id": "nope"})
    assert r.status == 400
    assert "last_event_id" in (await r.json())["error"]
    r = await live.get("/api/v1/events", headers={"Last-Event-ID": "-1"})
    assert r.status == 400
    r = await live.get("/api/v1/events", params={"limit": "0"})
    assert r.status == 400
    r = await live.get("/api/v1/events", params={"order_id": ""})
    assert r.status == 400
    # после ошибок поток штатно открывается
    resp, reader = await open_stream(live)
    try:
        await reader.read_more(want_comments=1)
    finally:
        resp.close()


async def test_client_close_does_not_leave_busy_loop(db, live_slow):
    resp, reader = await open_stream(live_slow)
    await reader.read_more(want_events=1)
    resp.close()
    await asyncio.sleep(0.1)
    # сервер не завис: остальные endpoint'ы отвечают, новые события пишутся
    assert (await live_slow.get("/stats")).status == 200
    assert await write_events(db, 1)
    resp, reader = await open_stream(live_slow)  # новый клиент тоже обслуживается
    try:
        await reader.read_more(want_events=1)
    finally:
        resp.close()


async def test_no_busy_loop_with_draining_client(db, live_slow):
    """Клиент не читает поток: сервер не крутится вхолостую (min-interval между
    опросами) и не блокирует event loop; после подключения события приходят."""
    import time
    await write_events(db, 3)
    t0 = time.monotonic()
    resp = await live_slow.get("/api/v1/events", params={"last_event_id": "0"})
    assert resp.status == 200
    ticks = []
    for _ in range(10):          # пишем, пока клиент молчит
        await write_events(db, 2)
        s = time.monotonic()
        await asyncio.sleep(0.02)
        ticks.append(time.monotonic() - s)
    assert time.monotonic() - t0 < 5      # всё выполнилось без зависания цикла
    assert max(ticks) < 0.5               # loop не голодает из-за потока
    resp.close()


async def test_dead_db_conn_503(db):
    """БД закрыта (деградация) — понятная ошибка вместо падения хендлера."""
    srv = await _server(build_app(db, event_stream=fast_stream(db)))
    cli = await _client(srv)
    try:
        await db.close()
        r = await cli.get("/api/v1/events")
        assert r.status == 503
        assert "error" in await r.json()
    finally:
        await cli.close()
        await srv.close()


async def test_existing_api_compat(live):
    """Обратная совместимость: старые (неверсионированные) endpoints живы."""
    r = await live.get("/orders")
    assert r.status == 200
    r = await live.get("/stats")
    assert r.status == 200
    r = await live.get("/approvals/open")
    assert r.status == 200
