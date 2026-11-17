
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

## Обход 06:23 — ВАЖНО
- РАЗОБРАНО: get_system_status/get_order/list_orders падали с 00:55 (relative import в script-режиме systemd), журнал врад ok (лог до возврата). ChatGPT ночью получал ошибки этих инструментов — объясняет его пассивность.
- Исправлено (dual-import) + деплой; проверка через туннель: isError=False, notices цепляются, unread=0.
- Notice ChatGPT с извинением и актуальным планом.

## Обход 07:23
- Фикс подействовал: ChatGPT активен с 06:27 (help, get_system_status ok, create_test_order ×3 — песочные пробы). Заказов боевых пока не ставит — прогревается.
- Моя notice-апология (1) висит непрочитанной — прикрепится к следующему его вызову.
- Зависших нет, сервисы active. dev без изменений (17eb49d + фиксы чекеров + mcp-фиксы).
## Обход 08:24 — тихо: зависших нет, сервисы active, ChatGPT молчит с 06:28 (владелец небуди).
## Обход 09:26 — тихо, сервисы active.
## Обход 10:26 — тихо, active.

## День: интеграция работы ChatGPT
- Его коммиты (93fc3d2 фронтенд SVG-DAG/safe-DOM + 4080026 строгий repo_changed) влиты в dev и задеплоены. Его repo_changed (контент-diff) заменил мою наивную версию — строже.
- Мерж: add/add в src/web (взял его), helpers _git_out/_repo_base_refs/_commits_since/_EMPTY_TREE до-вставлены, /ui/ index-роут, тесты приведены. 140/140.
- Notice ChatGPT: интеграция завершена, P3a-3 его ход.
## Обход 11:33 — тихо. ChatGPT активен 11:31 (пробы). active.
## P3a-3 (DeepSeek): доступ к /ui/ снаружи
- Публичного URL для /ui/ нет: API только 127.0.0.1:9310, Quick Tunnel ведёт на MCP :9320 (/ui/ там 404, /mcp 401). Docker nginx — VPN-контур, Дирижёра не проксирует.
- Добавлен опциональный read-only профиль build_app(public_ui_only=True) (только GET /ui + /orders,/stats,/approvals/open; мутации и SSE -> 403). Дефолт False, production не менялся. tests/test_ui_access.py (9 тестов).
- docs/ui-access.md: карта, threat model, rollout-чек-лист, инструкция. Ждёт ревью Kimi; нужны Cloudflare Access + named tunnel + CLI-флаги инстанса :9311.
- Прогон: 149/149 (с HOME/GOCACHE); без них 2 падения чекеров — окружение.
