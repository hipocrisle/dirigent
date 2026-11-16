"""Реальный smoke OpenCode: 1 запрос (~1 цент). В CI НЕ запускается.

Запуск вручную: .venv/bin/python -m pytest tests/test_real_smoke.py -q
"""
from __future__ import annotations

import asyncio

from src.executors import opencode
from src.models import StepSpec


def test_opencode_real_smoke_odno_slovo():
    shag = StepSpec(id="smoke", type="opencode", prompt="Ответь одним словом: ок",
                    timeout_seconds=180.0)
    res = asyncio.run(opencode.OpencodeExecutor().run(shag))
    assert res.ok, res.error_text
