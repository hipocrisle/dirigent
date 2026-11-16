"""SQLite-хранилище: WAL, транзакционные назначения, журнал событий."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from .models import OrderSpec, StepResult, StepSpec, new_id, now
from .states import OrderState, StepState, can_order, can_step

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  budget_max_usd REAL,
  repo TEXT,
  state TEXT NOT NULL DEFAULT 'new',
  spec_json TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  order_id TEXT NOT NULL REFERENCES orders(id),
  id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  type TEXT NOT NULL,
  executor TEXT,
  spec_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  backoff_until REAL,
  lease_owner TEXT,
  lease_until REAL,
  quota_reset_at REAL,
  result_json TEXT,
  error_class TEXT,
  error_text TEXT,
  updated REAL NOT NULL,
  PRIMARY KEY (order_id, id)
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  order_id TEXT,
  step_id TEXT,
  kind TEXT NOT NULL,
  data_json TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- open|approved|rejected|timeout
  default_action TEXT NOT NULL DEFAULT 'wait',
  expires_at REAL,
  created REAL NOT NULL,
  resolved REAL
);
CREATE INDEX IF NOT EXISTS idx_steps_state ON steps(state);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, seq);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def event(self, kind: str, order_id: str | None = None,
                    step_id: str | None = None, data: dict | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO events(ts, order_id, step_id, kind, data_json) VALUES(?,?,?,?,?)",
            (now(), order_id, step_id, kind, json.dumps(data or {})),
        )
        await self.conn.commit()

    # ── orders ──────────────────────────────────────────────
    async def create_order(self, spec: OrderSpec) -> str:
        oid = spec.id or new_id("ord")
        t = now()
        await self.conn.execute(
            "INSERT INTO orders(id, goal, priority, budget_max_usd, repo, state, spec_json, created, updated)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (oid, spec.goal, spec.priority, spec.budget_max_usd, spec.repo,
             OrderState.new.value, spec.model_dump_json(), t, t),
        )
        for s in spec.steps:
            await self.conn.execute(
                "INSERT INTO steps(order_id, id, state, type, executor, spec_json, max_attempts, updated)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (oid, s.id, StepState.pending.value, s.type, s.executor,
                 s.model_dump_json(), s.retry.max_attempts, t),
            )
        await self.event("order.created", oid, data={"goal": spec.goal, "steps": len(spec.steps)})
        await self.conn.commit()
        return oid

    async def get_order_state(self, oid: str) -> OrderState | None:
        cur = await self.conn.execute("SELECT state FROM orders WHERE id=?", (oid,))
        row = await cur.fetchone()
        return OrderState(row["state"]) if row else None

    async def transition_order(self, oid: str, to: OrderState) -> bool:
        cur = await self.conn.execute("SELECT state FROM orders WHERE id=?", (oid,))
        row = await cur.fetchone()
        if not row:
            return False
        frm = OrderState(row["state"])
        if not can_order(frm, to):
            return False
        await self.conn.execute(
            "UPDATE orders SET state=?, updated=? WHERE id=?", (to.value, now(), oid))
        await self.event("order.transition", oid, data={"from": frm.value, "to": to.value})
        await self.conn.commit()
        return True

    # ── steps ─────────────────────────────────────────────────
    async def get_step_spec(self, oid: str, sid: str) -> StepSpec:
        cur = await self.conn.execute(
            "SELECT spec_json FROM steps WHERE order_id=? AND id=?", (oid, sid))
        row = await cur.fetchone()
        return StepSpec.model_validate_json(row["spec_json"])

    async def transition_step(self, oid: str, sid: str, to: StepState,
                              force: bool = False, **fields) -> bool:
        """force=True — только для восстановления после краха демона (спека §6.4):
        шаг с протухшим lease легитимно возвращается в очередь."""
        cur = await self.conn.execute(
            "SELECT state FROM steps WHERE order_id=? AND id=?", (oid, sid))
        row = await cur.fetchone()
        if not row:
            return False
        frm = StepState(row["state"])
        if not force and not can_step(frm, to):
            return False
        sets = ["state=?", "updated=?"]
        vals: list = [to.value, now()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals += [oid, sid]
        await self.conn.execute(
            f"UPDATE steps SET {', '.join(sets)} WHERE order_id=? AND id=?", vals)
        kind = "step.transition" if not force else "step.recovered"
        await self.event(kind, oid, sid,
                         data={"from": frm.value, "to": to.value, **fields})
        await self.conn.commit()
        return True

    async def promote_pending(self) -> list[tuple[str, str]]:
        """pending -> ready, если все зависимости completed; -> cancelled при failed/cancelled deps."""
        cur = await self.conn.execute(
            "SELECT order_id, id, spec_json, state FROM steps WHERE state='pending'")
        rows = await cur.fetchall()
        moved: list[tuple[str, str]] = []
        for r in rows:
            spec = StepSpec.model_validate_json(r["spec_json"])
            if not spec.depends_on:
                ok = await self.transition_step(r["order_id"], r["id"], StepState.ready)
            else:
                q = ("SELECT id, state FROM steps WHERE order_id=? AND id IN (%s)"
                     % ",".join("?" * len(spec.depends_on)))
                dep_rows = await (await self.conn.execute(
                    q, (r["order_id"], *spec.depends_on))).fetchall()
                states = {d["id"]: StepState(d["state"]) for d in dep_rows}
                if all(states.get(d) == StepState.completed for d in spec.depends_on):
                    ok = await self.transition_step(r["order_id"], r["id"], StepState.ready)
                elif any(states.get(d) in (StepState.failed, StepState.cancelled)
                         for d in spec.depends_on):
                    ok = await self.transition_step(r["order_id"], r["id"], StepState.cancelled)
                    if ok:
                        await self.event("step.cascade_cancel", r["order_id"], r["id"])
                else:
                    ok = False
            if ok:
                moved.append((r["order_id"], r["id"]))
        return moved

    async def lease_step(self, oid: str, sid: str, owner: str,
                         lease_seconds: float) -> bool:
        """Атомарно: только ready (и backoff истёк) -> running с lease."""
        t = now()
        cur = await self.conn.execute(
            "UPDATE steps SET state=?, lease_owner=?, lease_until=?, attempts=attempts+1,"
            " updated=? WHERE order_id=? AND id=? AND state='ready'"
            " AND (backoff_until IS NULL OR backoff_until<=?)",
            (StepState.running.value, owner, t + lease_seconds, t, oid, sid, t))
        await self.conn.commit()
        if cur.rowcount == 1:
            await self.event("step.lease", oid, sid,
                             data={"owner": owner, "until": t + lease_seconds})
            return True
        return False

    async def extend_lease(self, oid: str, sid: str, owner: str,
                           lease_seconds: float) -> None:
        await self.conn.execute(
            "UPDATE steps SET lease_until=?, updated=? WHERE order_id=? AND id=?"
            " AND lease_owner=? AND state='running'",
            (now() + lease_seconds, now(), oid, sid, owner))
        await self.conn.commit()

    async def finish_step(self, oid: str, sid: str, result: StepResult) -> bool:
        """running -> completed/failed/waiting_quota/retry_scheduled по результату."""
        spec = await self.get_step_spec(oid, sid)
        cur = await self.conn.execute(
            "SELECT attempts FROM steps WHERE order_id=? AND id=?", (oid, sid))
        attempts = (await cur.fetchone())["attempts"]
        common = {"result_json": result.model_dump_json(),
                  "error_class": result.error_class.value,
                  "error_text": result.error_text,
                  "lease_owner": None, "lease_until": None}
        if result.ok:
            return await self.transition_step(oid, sid, StepState.completed, **common)
        if result.error_class.value == "quota":
            return await self.transition_step(
                oid, sid, StepState.waiting_quota,
                quota_reset_at=result.quota_reset_at, **common)
        if attempts < spec.retry.max_attempts:
            return await self.transition_step(
                oid, sid, StepState.retry_scheduled,
                backoff_until=now() + spec.retry.backoff_seconds, **common)
        return await self.transition_step(oid, sid, StepState.failed, **common)

    async def ready_steps(self, limit: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT order_id, id, type, executor FROM steps WHERE state='ready'"
            " AND (backoff_until IS NULL OR backoff_until<=?) ORDER BY updated LIMIT ?",
            (now(), limit))
        return await cur.fetchall()

    async def stale_leases(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT order_id, id, attempts, max_attempts FROM steps"
            " WHERE state='running' AND lease_until IS NOT NULL AND lease_until<?",
            (now(),))
        return await cur.fetchall()

    async def due_retries(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT order_id, id FROM steps WHERE state='retry_scheduled'"
            " AND backoff_until IS NOT NULL AND backoff_until<=?", (now(),))
        return await cur.fetchall()

    async def waiting_quota_steps(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT order_id, id, type, executor, quota_reset_at FROM steps WHERE state='waiting_quota'")
        return await cur.fetchall()

    async def steps_of(self, oid: str) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM steps WHERE order_id=? ORDER BY rowid", (oid,))
        return await cur.fetchall()

    # ── approvals ─────────────────────────────────────────────
    async def create_approval(self, oid: str, sid: str, question: str,
                              default_action: str, timeout_s: float) -> str:
        aid = new_id("apr")
        await self.conn.execute(
            "INSERT INTO approvals(id, order_id, step_id, question, status, default_action,"
            " expires_at, created) VALUES(?,?,?,?,?,?,?,?)",
            (aid, oid, sid, question, "open", default_action,
             now() + timeout_s if timeout_s > 0 else None, now()))
        await self.event("approval.created", oid, sid, {"approval": aid, "q": question})
        await self.conn.commit()
        return aid

    async def resolve_approval(self, aid: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE approvals SET status=?, resolved=? WHERE id=?", (status, now(), aid))
        await self.conn.commit()

    async def expired_approvals(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM approvals WHERE status='open' AND expires_at IS NOT NULL AND expires_at<?",
            (now(),))
        return await cur.fetchall()

    # ── восстановление ────────────────────────────────────────
    async def requeue_stale(self) -> list[tuple[str, str, str]]:
        """Зависшие lease -> ready (если попытки остались) или failed. Возвращает решения."""
        out = []
        for r in await self.stale_leases():
            oid, sid = r["order_id"], r["id"]
            if r["attempts"] < r["max_attempts"]:
                ok = await self.transition_step(
                    oid, sid, StepState.ready, force=True, lease_owner=None, lease_until=None,
                    backoff_until=now() + 1.0)
                out.append((oid, sid, "requeued" if ok else "skip"))
            else:
                ok = await self.transition_step(
                    oid, sid, StepState.failed, force=True, lease_owner=None, lease_until=None,
                    error_text="lease lost, attempts exhausted")
                out.append((oid, sid, "failed" if ok else "skip"))
        return out

    async def requeue_orphaned(self) -> None:
        """После краха демона: running без lease (демон умер до записи lease) -> ready."""
        cur = await self.conn.execute(
            "SELECT order_id, id, attempts, max_attempts FROM steps WHERE state='running'")
        for r in await cur.fetchall():
            if r["attempts"] < r["max_attempts"]:
                await self.transition_step(
                    r["order_id"], r["id"], StepState.ready, force=True,
                    lease_owner=None, lease_until=None, backoff_until=now() + 1.0)
            else:
                await self.transition_step(
                    r["order_id"], r["id"], StepState.failed, force=True,
                    lease_owner=None, lease_until=None,
                    error_text="orchestrator died mid-run")

    async def order_progress(self, oid: str) -> dict:
        cur = await self.conn.execute(
            "SELECT state, COUNT(*) n FROM steps WHERE order_id=? GROUP BY state", (oid,))
        by = {r["state"]: r["n"] for r in await cur.fetchall()}
        total = sum(by.values())
        done = by.get("completed", 0)
        if done == total and total > 0:
            verdict = "all_done"
        elif by.get("failed", 0) > 0:
            verdict = "has_failed"
        elif by.get("cancelled", 0) > 0:
            verdict = "has_cancelled"
        else:
            verdict = "in_progress"
        return {"by_state": by, "total": total, "done": done, "verdict": verdict}
