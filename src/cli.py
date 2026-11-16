"""CLI Дирижёра: submit / daemon / status / approve / cancel."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .brain import get_brain
from .db import DB
from .scheduler import Scheduler
from .states import OrderState

DB_PATH = "/matrix/dirigent/dirigent.db"


async def cmd_submit(args) -> None:
    db = DB(DB_PATH)
    await db.open()
    brain = get_brain()
    spec = brain.plan(args.order, repo=args.repo)
    oid = await db.create_order(spec)
    await db.transition_order(oid, OrderState.active)
    print(oid)
    await db.close()


async def cmd_daemon(args) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db = DB(DB_PATH)
    await db.open()
    sched = Scheduler(db, max_parallel=args.parallel)
    try:
        await sched.run_forever(tick=1.0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await sched.shutdown()
        await db.close()


async def cmd_status(args) -> None:
    db = DB(DB_PATH)
    await db.open()
    if args.order:
        prog = await db.order_progress(args.order)
        steps = await db.steps_of(args.order)
        print(json.dumps({"order": args.order, **prog}, ensure_ascii=False, indent=1))
        for s in steps:
            print(f"  {s['id']:<24} {s['state']:<18} att={s['attempts']} err={s['error_class'] or '-'} {s['error_text'] or ''}")
    else:
        cur = await db.conn.execute("SELECT id, goal, state FROM orders ORDER BY created DESC LIMIT 20")
        for r in await cur.fetchall():
            print(f"{r['id']}  {r['state']:<14}  {r['goal'][:70]}")
    await db.close()


async def cmd_approve(args) -> None:
    db = DB(DB_PATH)
    await db.open()
    sched = Scheduler(db)
    if args.reject:
        await sched.reject(args.approve)
        print("rejected")
    else:
        await sched.approve(args.approve)
        print("approved")
    await db.close()


async def cmd_cancel(args) -> None:
    db = DB(DB_PATH)
    await db.open()
    ok = await db.transition_order(args.cancel, OrderState.cancelled)
    # каскадно гасим шаги
    for s in await db.steps_of(args.cancel):
        from .states import StepState, TERMINAL_STEPS
        st = StepState(s["state"])
        if st not in TERMINAL_STEPS:
            await db.transition_step(args.cancel, s["id"], StepState.cancelled)
    print("cancelled" if ok else "no transition")
    await db.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="dirigent")
    p.add_argument("--db", default=DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("order", help="путь к YAML заказа или свободная формулировка (с brain=kimi)")
    s.add_argument("--repo")

    d = sub.add_parser("daemon")
    d.add_argument("--parallel", type=int, default=2)

    st = sub.add_parser("status")
    st.add_argument("order", nargs="?")

    a = sub.add_parser("approve")
    a.add_argument("approve", help="approval id")
    a.add_argument("--reject", action="store_true")

    c = sub.add_parser("cancel")
    c.add_argument("cancel", help="order id")

    args = p.parse_args()
    globals()["DB_PATH"] = args.db
    asyncio.run(globals()[f"cmd_{args.cmd}"](args))


if __name__ == "__main__":
    main()
