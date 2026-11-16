"""P3: SSE-поток журнала событий (GET /api/v1/events).

Источник истины — существующая таблица `events` (seq AUTOINCREMENT = монотонный
event ID). Второго журнала нет: snapshot для панели берётся из обычных REST
endpoints, SSE доставляет только изменения.

Гарантии:
- упорядоченная выдача по seq; курсор двигается только после успешной записи
  кадра клиенту — пропусков и дублей нет;
- Last-Event-ID (заголовок) или ?last_event_id= — клиент получает строго seq > cursor,
  при расхождении берётся более новый курсор;
- даунтайм не теряет события: reconnect с последним полученным id продолжает ровно
  с этого места (события, записанные пока клиент был отключён, выдаются первыми);
- heartbeat каждые ~15с при отсутствии данных — SSE-комментарий `: heartbeat`;
- ограничения: размер страницы, число событий на один ответ до явного yield
  (BATCH_LIMIT), между батчами — явный yield event loop (без голодания других
  корутин), min-interval между запросами к БД и idle-poll при пустом окне —
  против busy-loop; TTL соединения закрывает поток штатно, клиент переподключается
  с Last-Event-ID.
"""
from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web

from .db import DB

# ── лимиты (безопасные дефолты, в тестах подставляются меньшие) ──────────
PAGE_LIMIT = 200          # строк за один SELECT
BATCH_LIMIT = 500         # событий на один ответ до явного yield клиенту
MIN_POLL_INTERVAL = 0.1   # сек между опросами при пустом окне (анти busy-loop)
IDLE_POLL_INTERVAL = 1.0  # когда окно пустое — опрос реже
STREAM_TTL = 300.0        # сек: после этого сервер закрывает поток (клиент переподключится)
HEARTBEAT_SECONDS = 15.0
RETRY_MS = 2000           # SSE retry hint
MAX_ORDER_FILTER = 80

_FIELDS = "seq, ts, order_id, step_id, kind, data_json"


def _opt_int(raw) -> int | None:
    """None — параметра нет, '' — пусто, -1 — мусор (400)."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return 0  # пустой Last-Event-ID равнозначен старту потока
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return -1
    return v


def parse_cursor(req: web.Request) -> tuple[int, str | None, int, str | None]:
    """(since_seq, order_id, limit, error). error — текст для 400."""
    since = 0
    # заголовок Last-Event-ID и query-параметр равноправны; курсор только растёт
    for raw in [*req.query.getall("last_event_id", []), req.headers.get("Last-Event-ID")]:
        v = _opt_int(raw)
        if v is None:
            continue
        if v < 0:
            return 0, None, 0, f"bad last_event_id: {raw!r}"
        since = max(since, v)

    limit = _opt_int(req.query.get("limit"))
    if limit is not None and limit < 1:
        return 0, None, 0, "bad limit: must be a positive integer"
    limit = min(limit or PAGE_LIMIT, PAGE_LIMIT)

    order_id = req.query.get("order_id")
    if order_id is not None:
        order_id = order_id.strip()
        if not order_id or len(order_id) > MAX_ORDER_FILTER:
            return 0, None, 0, "bad order_id"
    return since, order_id or None, limit, None


def public_event(r) -> dict:
    """Стабильный контракт события: id (seq), type (kind), ts, order_id, step_id, data."""
    try:
        data = json.loads(r["data_json"] or "{}")
    except Exception:
        data = {}
    return {"id": r["seq"], "type": r["kind"], "ts": r["ts"],
            "order_id": r["order_id"], "step_id": r["step_id"], "data": data}


def format_event(ev: dict) -> bytes:
    payload = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
    return f"id: {ev['id']}\nevent: {ev['type']}\ndata: {payload}\n\n".encode()


async def fetch_after(db: DB, since: int, order_id: str | None, limit: int) -> list:
    if order_id:
        cur = await db.conn.execute(
            f"SELECT {_FIELDS} FROM events WHERE seq>? AND order_id=?"
            " ORDER BY seq LIMIT ?", (since, order_id, limit))
    else:
        cur = await db.conn.execute(
            f"SELECT {_FIELDS} FROM events WHERE seq>? ORDER BY seq LIMIT ?",
            (since, limit))
    return await cur.fetchall()


class EventStream:
    """Handler для GET /api/v1/events. Зависимости инжектируются для тестов."""

    def __init__(self, db: DB, *, heartbeat: float = HEARTBEAT_SECONDS,
                 stream_ttl: float = STREAM_TTL,
                 min_poll: float = MIN_POLL_INTERVAL,
                 idle_poll: float = IDLE_POLL_INTERVAL,
                 page_limit: int = PAGE_LIMIT,
                 batch_limit: int = BATCH_LIMIT):
        self.db = db
        self.heartbeat = heartbeat
        self.stream_ttl = stream_ttl
        self.min_poll = min_poll
        self.idle_poll = idle_poll
        self.page_limit = min(page_limit, PAGE_LIMIT)
        self.batch_limit = batch_limit

    async def __call__(self, req: web.Request) -> web.StreamResponse:
        since, order_id, limit, err = parse_cursor(req)
        if err:
            return web.json_response({"error": err}, status=400)
        if self.db.conn is None:
            return web.json_response({"error": "db not ready"}, status=503)

        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(req)
        try:
            await resp.write(
                f"retry: {RETRY_MS}\n: connected id={since}\n\n".encode())
            await self._pump(resp, since, order_id, limit)
        finally:
            # штатное закрытие/обрыв (клиент ушёл: ConnectionResetError и т.п.):
            # write_eof не должен маскировать исходное исключение
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    async def _pump(self, resp: web.StreamResponse, since: int,
                    order_id: str | None, limit: int) -> None:
        loop = asyncio.get_running_loop()
        cursor = since
        sent = 0
        now = loop.time()
        last_write = now
        last_poll = 0.0
        deadline = now + self.stream_ttl
        while True:
            now = loop.time()
            if now >= deadline:
                return  # штатное закрытие: клиент переподключится с Last-Event-ID
            wait = min(max(0.0, self.min_poll - (now - last_poll)),
                       max(0.0, deadline - now))
            if wait > 0:
                await asyncio.sleep(wait)
                continue

            rows = await fetch_after(self.db, cursor, order_id, limit)
            last_poll = loop.time()
            if not rows:
                if last_poll - last_write >= self.heartbeat:
                    await resp.write(b": heartbeat\n\n")
                    last_write = last_poll
                else:
                    # idle-interval, но не дольше, чем до TTL/heartbeat
                    await asyncio.sleep(min(self.idle_poll, self.heartbeat,
                                            max(0.0, deadline - last_poll)))
                continue

            for r in rows:
                await resp.write(format_event(public_event(r)))
                cursor = r["seq"]  # курсор двигается только после успешной записи
                sent += 1
                if sent >= self.batch_limit:
                    sent = 0
                    await asyncio.sleep(0)  # отдаём event loop другим задачам
            last_write = loop.time()
            await asyncio.sleep(0)  # yield после каждой пачки (без голодания loop)
