"""HTTP API Дирижёра: 127.0.0.1:9310, loopback-only (AD-6).

Единая точка доступа для CLI, Telegram-бота и будущего TUI: ядро не меняется,
TUI подключается поверх этих же эндпоинтов (требование круга 4).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from aiohttp import web

from . import actions, notify
from .db import DB
from .event_stream import EventStream
from .states import StepState

DB_PATH = "/matrix/dirigent/dirigent.db"

# P3a-2: статический read-only визуализатор. Только GET, никаких мутаций.
WEB_DIR = Path(__file__).resolve().parent / "web"


async def _index_redirect(_req) -> web.Response:
    """Корень -> /ui/ (удобно открыть в браузере)."""
    raise web.HTTPFound("/ui/")


async def _ui_redirect(_req) -> web.Response:
    """Без слэша -> со слэшем, чтобы относительные ссылки в index.html работали."""
    raise web.HTTPFound("/ui/")


async def _ui_index(_req) -> web.FileResponse:
    """index.html как точка входа статического интерфейса."""
    return web.FileResponse(WEB_DIR / "index.html")


def _step_public(r) -> dict:
    out = {"id": r["id"], "state": r["state"], "type": r["type"],
           "attempts": r["attempts"], "error_class": r["error_class"],
           "error_text": (r["error_text"] or "")[:300]}
    if r["result_json"]:
        try:
            tail = json.loads(r["result_json"]).get("log_tail", "")
            if tail:
                out["log_tail"] = tail
        except Exception:
            pass
    return out


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

    async def open_approvals(self, _req) -> web.Response:
        cur = await self.db.conn.execute(
            "SELECT a.id, a.order_id, a.step_id, a.question, a.expires_at, o.goal"
            " FROM approvals a JOIN orders o ON o.id = a.order_id"
            " WHERE a.status='open' ORDER BY a.created")
        return web.json_response([dict(r) for r in await cur.fetchall()])

    async def _approval_decision(self, req, verdict: str) -> web.Response:
        """Однократное решение с понятной причиной отказа (кнопки TG, /approve).

        Ответ всегда JSON:
          ok=True            — решение впервые применено;
          state='expired'    — срок истёк, решение по умолчанию (кнопки мертвы);
          state='approved'/'rejected' — уже решено ранее, повтор не применяется;
          state='missing'    — апрува нет (404).
        ok (принято ли действие над шагом) отделён от state (итог апрува).
        """
        aid = req.match_info["aid"]
        row = await (await self.db.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (aid,))).fetchone()
        if not row:
            return web.json_response({"ok": False, "state": "missing",
                                      "error": "approval not found"}, status=404)
        base = {"order_id": row["order_id"], "step_id": row["step_id"]}
        state = str(row["status"])
        if state == "open" and row["expires_at"] is not None and \
                float(row["expires_at"]) < time.time():
            state = "expired"
        if state != "open":
            return web.json_response({"ok": False, "state": state, **base})
        applied = await (actions.approve(self.db, aid) if verdict == "approve"
                         else actions.reject(self.db, aid))
        if not applied:
            # гонка: параллельный клик забрал апрув первым
            again = await (await self.db.conn.execute(
                "SELECT status FROM approvals WHERE id=?", (aid,))).fetchone()
            state = str(again["status"]) if again else "missing"
            return web.json_response({"ok": False, "state": state, **base})
        notify.mark_approval_decided(aid, verdict)
        await notify.acknowledge_approval_cards(aid, verdict)
        return web.json_response({"ok": True, "state": "approved" if verdict == "approve"
                                  else "rejected", **base})

    async def create_order(self, req) -> web.Response:
        """MCP/ChatGPT: создание реального заказа. Allowlist + бюджет + idempotency."""
        from .models import OrderSpec
        from .policy import order_budget_ok, repo_allowed
        from .states import OrderState
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        request_id = (body.get("request_id") or "")[:80]
        data = body.get("order") or {}
        repo = data.get("repo")
        if not repo_allowed(repo):
            await self.db.event("order.refused", None,
                                data={"reason": "repo_not_allowed", "repo": repo})
            return web.json_response(
                {"error": f"repo not allowed: {repo}. allowlist only"}, status=403)
        if not order_budget_ok(data.get("budget_max_usd")):
            return web.json_response(
                {"error": "budget over MCP cap, owner confirmation needed"},
                status=403)
        try:
            spec = OrderSpec.model_validate(data)
        except Exception as e:
            return web.json_response({"error": f"invalid order: {e}"}, status=400)
        oid, created = await self.db.create_order_idem(request_id, spec)
        if created:
            await self.db.transition_order(oid, OrderState.active)
        return web.json_response({"order_id": oid, "created": created,
                                  "duplicate": not created})

    async def retry_step(self, req) -> web.Response:
        ok = await actions.retry_step(self.db, req.match_info["oid"],
                                      req.match_info["sid"])
        return web.json_response({"ok": ok})

    async def diagnose_step(self, req) -> web.Response:
        from .procmon import diagnose
        oid, sid = req.match_info["oid"], req.match_info["sid"]
        repo = await self.db.get_order_repo(oid)
        return web.json_response(diagnose(oid, sid, repo))

    async def kill_step(self, req) -> web.Response:
        from .states import StepState
        from .procmon import kill_processes
        oid, sid = req.match_info["oid"], req.match_info["sid"]
        cur = await self.db.conn.execute(
            "SELECT state FROM steps WHERE order_id=? AND id=?", (oid, sid))
        row = await cur.fetchone()
        if not row:
            return web.json_response({"error": "not found"}, status=404)
        if StepState(row["state"]) != StepState.running:
            return web.json_response({"ok": False, "error": "step not running",
                                      "state": row["state"]})
        result = await __import__("asyncio").to_thread(kill_processes, oid, sid)
        ok = await self.db.transition_step(
            oid, sid, StepState.cancelled, error_text="killed via api",
            lease_owner=None, lease_until=None)
        await self.db.event("step.killed", oid, sid, data=result)
        return web.json_response({"ok": ok, **result})

    async def approve(self, req) -> web.Response:
        return await self._approval_decision(req, "approve")

    async def reject(self, req) -> web.Response:
        return await self._approval_decision(req, "reject")

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


# P3a-3: профиль для публикации /ui/ за аутентифицирующим прокси. Прокси
# обязан проверять владельца (Cloudflare Access/IdP); приложение дополнительно
# не отдаёт наружу мутации и SSE — только GET-чтение.
PUBLIC_UI_SAFE_GET = frozenset({"/", "/ui", "/ui/", "/orders", "/stats",
                                "/approvals/open"})
# Мутации и SSE: /orders/{oid}/steps/{sid}/{retry,kill,diagnose},
# /orders/{oid}/{pause,resume,cancel}, /approvals/{aid}/{approve,reject},
# POST /orders, GET /api/v1/events — наружу через публичный профиль не идут.


def _public_ui_guard_ok(req) -> bool:
    if req.method not in ("GET", "HEAD"):
        return False
    if req.path in PUBLIC_UI_SAFE_GET:
        return True
    return req.path.startswith("/ui/")


@web.middleware
async def _public_ui_read_only(request, handler):
    if not _public_ui_guard_ok(request):
        return web.json_response({"error": "public_ui_read_only"}, status=403)
    return await handler(request)


def build_app(db: DB, *, event_stream: EventStream | None = None,
              public_ui_only: bool = False) -> web.Application:
    """event_stream=None — дефолтный SSE-поток; в тестах можно подставить лимиты.

    public_ui_only=True — профиль для внешнего /ui/-доступа (за прокси с auth):
    только GET-чтение UI/существующего REST, без мутаций и SSE.
    """
    api = Api(db)
    sse = event_stream or EventStream(db)
    app = web.Application(middlewares=[_public_ui_read_only] if public_ui_only else [])
    async def _ui_index(req):
        return web.FileResponse("/matrix/dirigent/src/web/index.html")
    app.router.add_get("/ui/", _ui_index)
    app.router.add_static("/ui/", path="/matrix/dirigent/src/web", show_index=True)
    # P3: версионированный API. Snapshot — через уже существующие REST endpoints
    # (/orders, /orders/{oid}, ...), SSE — только поток изменений из таблицы events.
    app.router.add_get("/api/v1/events", sse)
    app.router.add_get("/orders", api.orders)
    app.router.add_get("/orders/{oid}", api.order_detail)
    app.router.add_get("/orders/{oid}/log", api.order_log)
    app.router.add_get("/approvals/open", api.open_approvals)
    app.router.add_post("/orders", api.create_order)
    app.router.add_post("/orders/{oid}/steps/{sid}/retry", api.retry_step)
    app.router.add_get("/orders/{oid}/steps/{sid}/diagnose", api.diagnose_step)
    app.router.add_post("/orders/{oid}/steps/{sid}/kill", api.kill_step)
    app.router.add_post("/approvals/{aid}/approve", api.approve)
    app.router.add_post("/approvals/{aid}/reject", api.reject)
    app.router.add_post("/orders/{oid}/pause", api.pause)
    app.router.add_post("/orders/{oid}/resume", api.resume)
    app.router.add_post("/orders/{oid}/cancel", api.cancel)
    app.router.add_get("/stats", api.stats)
    app.router.add_get("/budget", budget_endpoint)
    # P3a-2: статический read-only интерфейс (GET-only). Не влияет на API/SSE.
    app.router.add_get("/", _index_redirect)
    app.router.add_get("/ui", _ui_redirect)
    app.router.add_get("/ui/", _ui_index)
    app.router.add_static("/ui/", WEB_DIR, follow_symlinks=False,
                          append_version=True)
    return app


async def serve(host: str = "127.0.0.1", port: int = 9310,
                db_path: str = DB_PATH,
                public_ui_only: bool = False) -> None:
    """Запуск API. public_ui_only — отдельный read-only инстанс для публикации
    /ui/ за аутентифицирующим прокси (не трогает боевой профиль на 9310)."""
    db = DB(Path(db_path))
    await db.open()
    app = build_app(db, public_ui_only=public_ui_only)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    import logging
    logging.getLogger("dirigent.api").info(
        "API on http://%s:%s%s", host, port,
        " (public_ui_only)" if public_ui_only else "")
    await __import__("asyncio").Event().wait()


async def budget_endpoint(req) -> web.Response:
    from .budget import report
    return web.json_response(report())
