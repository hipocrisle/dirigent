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
    from . import actions
    if args.reject:
        ok = await actions.reject(db, args.approve)
    else:
        ok = await actions.approve(db, args.approve)
    print("ok" if ok else "no-op")
    await db.close()


async def cmd_cancel(args) -> None:
    db = DB(DB_PATH)
    await db.open()
    from . import actions
    ok = await actions.cancel_order(db, args.cancel)
    print("cancelled" if ok else "no transition")
    await db.close()


async def cmd_pause(args) -> None:
    db = DB(DB_PATH)
    await db.open()
    from . import actions
    ok = await actions.pause_order(db, args.pause) if not args.resume else await actions.resume_order(db, args.pause)
    print("ok" if ok else "no-op")
    await db.close()


async def cmd_api(args) -> None:
    from .api import serve
    await serve(host=args.host, port=args.port, db_path=DB_PATH,
                public_ui_only=args.public_ui_only)


def build_parser() -> argparse.ArgumentParser:
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

    pa = sub.add_parser("pause")
    pa.add_argument("pause", help="order id")
    pa.add_argument("--resume", action="store_true")

    ap = sub.add_parser("api")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9310)
    # Публикация /ui/ наружу — только вместе с аутентифицирующим прокси.
    # Флаг включает read-only профиль: мутации и SSE приложения отдают 403.
    ap.add_argument("--public-ui-only", action="store_true",
                    help="read-only профиль для публикации /ui/ за прокси с auth")
    return p


def main() -> None:
    args = build_parser().parse_args()
    globals()["DB_PATH"] = args.db
    asyncio.run(globals()[f"cmd_{args.cmd}"](args))


if __name__ == "__main__":
    main()
