from __future__ import annotations

import pytest

from src.db import DB
from src.scheduler import Scheduler


@pytest.fixture
async def db(tmp_path):
    d = DB(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def sched(db):
    return Scheduler(db, max_parallel=2)


async def run_ticks(sched: Scheduler, n: int, delay: float = 0.05) -> None:
    import asyncio
    for _ in range(n):
        await sched.tick()
        await asyncio.sleep(delay)


def yaml_order(steps: list[dict], goal: str = "test", **kw) -> str:
    import yaml
    return yaml.safe_dump({"goal": goal, "steps": steps, **kw},
                          allow_unicode=True)
