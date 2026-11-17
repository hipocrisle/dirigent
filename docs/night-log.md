
## Обход 02:23
- Зависших running: нет. Сервисы: все active.
- ord_18a2cf362478 (ChatGPT, fix repo_changed): провал из-за БАГА чекеров, не исполнителя.
- Исправлено: check_pytest ищет .venv в родителях worktree; repo_changed = dirty ИЛИ коммиты поверх базы (agent-commit false negative устранён). 100/100 → деплой.
- integrate (SSE P3a-1): 112/112, влито в dev, задеплоено, /api/v1/events жив на loopback.
- Notice ChatGPT: причины провалов + инструкция P3a-2b.

## Обход 03:23
- ChatGPT ночью молчит (notices не читал, заказов нет). Критический путь взял на себя.
- P3a-2 сделан Kimi напрямую: src/web/ (index, order, app.js, order.js, style.css), static /ui/ в api.py, тест на отдачу. 116/116.
- integrate → dev задеплоено; /ui/ 200, /api/v1/events отдаёт text/event-stream.
- Notice ChatGPT: P3a-2b не ставить; дальше P3a-3 приёмка или браузер после сброса лимитов Codex.

## Обход 04:23
- ChatGPT ожил 03:27 (dirigent_call/list_orders/get_logs) — работает осторожно, заказов не ставит.
- Notices теперь доставляются и через list_orders (его любимый полл). Notice: P3a-2 готов, дальше приёмка или браузер.
- Зависших процессов нет, dev на 17eb49d, сервисы active.

## Обход 05:23
- ChatGPT активен (04:26-28: help FAIL-вызовы, get_system_status/list_orders ok). Заказов не ставит.
- Notices: механизм проверен напрямую — работает; возможно его 04:28 вызов шёл по старому процессу. Перепостил сводку свежей.
- Зависших нет, сервисы active. Дев не менялся.
