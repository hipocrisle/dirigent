"""HTTP API Дирижёра: 127.0.0.1:9310, loopback-only (AD-6).

Единая точка доступа для CLI, Telegram-бота и будущего TUI: ядро не меняется,
TUI подключается поверх этих же эндпоинтов (требование круга 4).
"""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from . import actions
from .db import DB
from .states import StepState

DB_PATH = "/matrix/dirigent/dirigent.db"


def _step_public(r) -> dict:
    return {"id": r["id"], "state": r["state"], "type": r["type"],
            "attempts": r["attempts"], "error_class": r["error_class"],
            "error_text": (r["error_text"] or "")[:300]}


class Api:
    def __init__(self, db: DB):
        self.db = db

    async def orders(self, _req) -> web.Response:
        cur = await self.db.conn.execute(
            "SELECT id, goal, state, priority FROM orders ORDER BY created DESC LIMIT 50")
        out = [dict(r) for r in await cur.fetchall()]
        for o in out:
            o["progress"] = await self.db.order_progress(o["id"])
        return web.json_response(out)

    async def order_detail(self, req) -> web.Response:
        oid = req.match_info["oid"]
        cur = await self.db.conn.execute("SELECT * FROM orders WHERE id=?", (oid,))
        row = await cur.fetchone()
        if not row:
            return web.json_response({"error": "not found"}, status=404)
        steps = [_step_public(r) for r in await self.db.steps_of(oid)]
        approvals = [dict(r) for r in await (await self.db.conn.execute(
            "SELECT id, step_id, question, status, expires_at FROM approvals"
            " WHERE order_id=? AND status='open'", (oid,))).fetchall()]
        return web.json_response({**dict(row), "spec": None,
                                  "steps": steps, "open_approvals": approvals})

    async def order_log(self, req) -> web.Response:
        oid = req.match_info["oid"]
        limit = int(req.query.get("limit", "50"))
        cur = await self.db.conn.execute(
            "SELECT ts, step_id, kind, data_json FROM events WHERE order_id=?"
            " ORDER BY seq DESC LIMIT ?", (oid, limit))
        rows = [{"ts": r["ts"], "step": r["step_id"], "kind": r["kind"],
                 "data": json.loads(r["data_json"] or "{}")} for r in await cur.fetchall()]
        return web.json_response(list(reversed(rows)))

    async def approve(self, req) -> web.Response:
        aid = req.match_info["aid"]
        ok = await actions.approve(self.db, aid)
        return web.json_response({"ok": ok})

    async def reject(self, req) -> web.Response:
        aid = req.match_info["aid"]
        ok = await actions.reject(self.db, aid)
        return web.json_response({"ok": ok})

    async def pause(self, req) -> web.Response:
        ok = await actions.pause_order(self.db, req.match_info["oid"])
        return web.json_response({"ok": ok})

    async def resume(self, req) -> web.Response:
        ok = await actions.resume_order(self.db, req.match_info["oid"])
        return web.json_response({"ok": ok})

    async def cancel(self, req) -> web.Response:
        ok = await actions.cancel_order(self.db, req.match_info["oid"])
        return web.json_response({"ok": ok})

    async def stats(self, _req) -> web.Response:
        return web.json_response(await actions.stats(self.db))


def build_app(db: DB) -> web.Application:
    api = Api(db)
    app = web.Application()
    app.router.add_get("/orders", api.orders)
    app.router.add_get("/orders/{oid}", api.order_detail)
    app.router.add_get("/orders/{oid}/log", api.order_log)
    app.router.add_post("/approvals/{aid}/approve", api.approve)
    app.router.add_post("/approvals/{aid}/reject", api.reject)
    app.router.add_post("/orders/{oid}/pause", api.pause)
    app.router.add_post("/orders/{oid}/resume", api.resume)
    app.router.add_post("/orders/{oid}/cancel", api.cancel)
    app.router.add_get("/stats", api.stats)
    return app


async def serve(host: str = "127.0.0.1", port: int = 9310,
                db_path: str = DB_PATH) -> None:
    db = DB(Path(db_path))
    await db.open()
    app = build_app(db)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    import logging
    logging.getLogger("dirigent.api").info("API on http://%s:%s", host, port)
    await __import__("asyncio").Event().wait()
