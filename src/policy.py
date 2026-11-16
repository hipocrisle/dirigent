"""Политика автономии по умолчанию (утверждена ChatGPT-руководителем, 17.11).

Консервативные защитные рамки, не оценка стоимости. unknown != $0:
где расход неизмерим — действуют лимиты вызовов/объёма.
"""
from __future__ import annotations

# Бюджеты
ORDER_BUDGET_USD_DEFAULT = 0.25     # на заказ без отдельного подтверждения
ORDER_BUDGET_USD_MAX = 1.0          # потолок для MCP-create без владельца
DAY_BUDGET_USD = 1.0
MONTH_BUDGET_USD = 10.0

# Попытки и планирование
MAX_STEP_LAUNCHES = 3               # жёсткий потолок запусков шага (включая retry)
MAX_REPLANS = 2

# Консервативные лимиты при неизмеримом расходе (unknown-usage исполнителей)
UNKNOWN_CALLS_PER_ORDER = 200
UNKNOWN_TOKENS_PER_CALL = 200_000

# Параллельность
MAX_PARALLEL_EXECUTORS = 2

# Allowlist репозиториев для MCP create_order (P0/P1)
ALLOWED_REPOS = [
    "/matrix/bench-pilot/repo",
]

DANGEROUS_VIA_TELEGRAM_ONLY = [
    "publications", "secrets", "system_install", "destructive_git",
    "data_deletion", "budget_override",
]


def repo_allowed(repo: str | None) -> bool:
    return repo is None or repo in ALLOWED_REPOS


def order_budget_ok(budget: float | None) -> bool:
    if budget is None:
        return True
    return budget <= ORDER_BUDGET_USD_MAX
