# Дирижёр (dirigent)

Персональный оркестратор автономных агентов: ставишь цель один раз — система
сама планирует, распределяет между исполнителями, обрабатывает квоты и лимиты,
восстанавливается после сбоев и пригоняет результат к machine-проверяемым
критериям. Человек подключается только там, где решение действительно его:
деньги, секреты, публикации, опасные действия — через неблокирующие согласования
в Telegram.

**English summary:** a lightweight personal orchestrator for LLM coding agents
(Codex CLI / OpenCode / interchangeable "brain"), with a strict step state
machine, SQLite-WAL job queue with leases, quota-aware scheduling, crash
recovery, machine-checkable acceptance criteria and non-blocking human
approvals. Python 3.10+, stdlib-first.

## Почему не готовый оркестратор

Ни один существующий инструмент (Paperclip, n8n, Temporal, OpenClaw и др.) не
покрывает связку «CLI-агенты + генеративные пайплайны + домашний GPU-ПК +
мессенджер как UI + сменный мозг». Здесь — минимальное ядро под эту задачу:
~1.5K строк Python, SQLite, systemd. Архитектурные идеи позаимствованы у
Paperclip (MIT, см. LICENSE).

## Устройство

Три уровня, жёстко разделённые:

1. **Brain** (сменный LLM-планировщик) — превращает цель в DAG шагов.
   Выбирается через `DIRIGENT_BRAIN`; ядро от него не зависит.
2. **Дирижёр** (этот код) — очередь, состояния, lease/heartbeat, квоты,
   ретраи, апрувы, восстановление. Никаких LLM-вызовов внутри.
3. **Исполнители** — адаптеры с единым интерфейсом (`Executor`): `codex`,
   `opencode`, `shell`, далее медиа (ComfyUI, ACE-Step).

Состояния шага: `pending → ready → running → completed | failed |
waiting_resource | waiting_approval | waiting_quota | retry_scheduled`.
Молчание человека никогда не означает согласия.

## Быстрый старт

```bash
python3 -m venv .venv && .venv/bin/pip install pydantic aiosqlite pyyaml pytest pytest-asyncio
.venv/bin/python -m pytest tests/ -q          # 64 теста
.venv/bin/python -m src.cli submit examples/order_smoke.yaml
.venv/bin/python -m src.cli daemon            # планировщик (systemd-unit в планах)
.venv/bin/python -m src.cli status <order_id>
```

### Веб-визуализатор (read-only, P3a-2)

Статический интерфейс под `src/web/` (HTML/CSS/vanilla JS, без сборки) раздаётся
демоном API на `http://127.0.0.1:9310/ui/` (корень перенаправляет туда).
Только `GET` к существующему REST (`/orders`, `/orders/{oid}`), без мутаций и SSE.
Показывает список заказов, деталку с DAG шагов и инспектор узла.
`prompt` и `depends_on` бэкенд пока не отдаёт — интерфейс показывает честную
заглушку «Недоступно через REST API».

## Статус

Активная разработка (ноябрь 2026). Готово: ядро (WP-1), чекеры приёмки (WP-3),
адаптеры codex/opencode с классификацией ошибок (WP-2). Дальше: ревью квота-логики,
Telegram-слой, TUI-дашборд, медиа-конвейер. Задачи и владение файлами — в
[TASKS.md](TASKS.md), архитектурные решения — в [PLAN.md](PLAN.md).

## Лицензия

MIT. Нет гарантий; код управляет внешними агентами — запускай осознанно.
