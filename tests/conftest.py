from __future__ import annotations

import pytest

from src.db import DB
from src.scheduler import Scheduler
from tests import notify_fixtures as nf


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path, monkeypatch):
    """Любой тест, дёрнувший src.notify, пишет в СВОЙ журнал, а не в прод.

    Иначе юнит-тест планировщика создаёт реальные pending-уведомления, и живой
    sender демона отправляет их владельцу (один из каналов флуда 17.11).
    """
    import importlib
    m = importlib.import_module("src.notify")
    monkeypatch.setattr(m, "JOURNAL_DB", str(tmp_path / "notify.db"), raising=False)
    monkeypatch.setattr(m, "MODE", "quiet", raising=False)


@pytest.fixture
async def db(tmp_path):
    d = DB(tmp_path / "t.db")
    await d.open()
    yield d
    await d.close()


@pytest.fixture
def sched(db):
    return Scheduler(db, max_parallel=2)


@pytest.fixture
def notify(tmp_path, monkeypatch):
    """Изолированный outbox с реальной схемой журнала (см. notify_fixtures)."""
    return nf.make_notify(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Ни один тест не может доставить в Telegram: token всегда пустой.

    Двойная защита к _allow_nonproduction_send: даже если тест явно разрешит
    отправку, без токена outbox останется локальным (инцидент 17.11 — флуд).
    """
    import importlib
    m = importlib.import_module("src.notify")
    monkeypatch.setattr(m, "_bot_token", lambda: None, raising=False)


@pytest.fixture
def tg(notify, monkeypatch):
    fake = nf.install_fake_tg(notify, monkeypatch)
    monkeypatch.setattr(notify, "_bot_token", lambda: "tok")
    return fake


async def run_ticks(sched: Scheduler, n: int, delay: float = 0.05) -> None:
    import asyncio
    for _ in range(n):
        await sched.tick()
        await asyncio.sleep(delay)


def yaml_order(steps: list[dict], goal: str = "test", **kw) -> str:
    import yaml
    return yaml.safe_dump({"goal": goal, "steps": steps, **kw},
                          allow_unicode=True)
