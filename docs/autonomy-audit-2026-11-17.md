# Аудит автономного цикла Dirigent и план интеграции

Адресат: владелец и ChatGPT. Дата: 17 ноября 2026. Выполнены аудит и ограниченные диагностические проверки. Рабочие заказы, генерация кода, деплой и изменения сервисов не запускались. Вертикальный цикл пока не соответствует критерию приёмки.

## Состояние репозитория и результаты заказов

Начальный снимок: `dev = origin/dev = 6be2eeed0124dc5a87bd918f4ec651e76a62482d`, основное дерево чистое. Во время аудита сторонняя сессия изменила `src/mcp_server.py` и создала `188aa12335a167b2ad12bb1567e892539b521832` (dual import get_budget). Этот коммит аудитор не создавал и не менял. Ветка `dirigent/integrate` стоит на `ee40b2f`; она не является свежей полной копией dev. Получение remote не выполнялось: origin/dev здесь локальный tracking ref, не проверка удалённого сервера.

`git worktree list --porcelain`: 20 зарегистрированных деревьев при итоговой проверке, включая основной checkout и detached `/tmp/opencode/basecheck` на `408e2df`. Ничего не удалено и не сброшено. Для трёх целевых деревьев:

| Заказ | Факт в Git | Факт MCP |
| --- | --- | --- |
| M1 `ord_994884cae636` | Дерево чистое, HEAD `d7d5213c39cdeb61a59e94cba96bb261cbca9ac0`. Коммит уже предок dev; интеграционные коммиты `ee40b2f`, `dcca548`. `dev..ветка` содержит 0 коммитов. | error / failed, 2 attempts, runtime acceptance: два теста Go cache. Ошибка заказа не означает отсутствия реализации. |
| M2 `ord_3fba66c5b6ed` | Дерево чистое, HEAD `de035874b6228db34f68374454eb223540a4a16d`, уже предок dev, 0 уникальных коммитов. Дополнительный select-only registry вошёл в `d86cd22`, инструкция в `5acb6d2`; wiring `071adcc`, совместимость `9932662`, `1f20dbe`, `51e9e18`. | error / failed, 2 attempts, auth. log_tail исполнителя сообщает `5acb6d2`; сырой error_text содержит вывод команд и не доказывает, что вся работа потеряна. |
| E4 `ord_c23fbd1832a5` | HEAD `6be2eee`; отдельного нового коммита нет. Изменены `src/scheduler.py`, `src/registry/scheduler.py`; новые `docs/e4-integration.md`, `tests/test_e4_integration.py`. Всё сохранено в исходном worktree. | error / failed, 2 attempts, runtime acceptance. Обе попытки: 315 passed / 2 failed. |

E1/E3 уже внесены в `64bb461`, supervisor E2 в `6be2eee`. Повторно генерировать эти модули нельзя: требуется интеграция и исправление границ ответственности.

## Разбор двух ошибок E4

Через существующий MCP получены get_order и get_logs для E4. Обе записи step.transition содержат одинаковые имена ошибок: `tests/test_checkers.py::test_go_test_ok`, `tests/test_checkers.py::test_run_acceptance_chain`. Первая попытка: 2 failed, 315 passed за 88.93 s; вторая: 2 failed, 315 passed за 84.17 s.

На неизменённом E4 worktree выполнены реальные вызовы Go через существующие checkers:

```sh
env -u HOME -u GOCACHE -u XDG_CACHE_HOME /matrix/dirigent/.venv/bin/python -m pytest tests/test_checkers.py::test_go_test_ok tests/test_checkers.py::test_run_acceptance_chain -q -p no:cacheprovider
# exit 1; 2 failed in 0.81s
# build cache is required, but could not be located:
# GOCACHE is not defined and neither $XDG_CACHE_HOME nor $HOME are defined

env GOCACHE=/tmp/dirigent-audit-gocache GOMAXPROCS=2 /matrix/dirigent/.venv/bin/python -m pytest tests/test_checkers.py::test_go_test_ok tests/test_checkers.py::test_run_acceptance_chain -q -p no:cacheprovider
# exit 0; 2 passed in 2.87s
```

Вывод: эти две ошибки воспроизводятся из-за окружения Go cache и устраняются его настройкой без изменения M1/M2/E4. Это дефект надёжности окружения приёмки, а не доказанный дефект логики E4. Полный исторический запуск 317 тестов повторно не подтверждён.

Дополнительно выполнен `timeout 45s env GOCACHE=/tmp/dirigent-audit-gocache GOMAXPROCS=2 /matrix/dirigent/.venv/bin/python -m pytest tests/test_e4_integration.py -q -p no:cacheprovider -o faulthandler_timeout=20`. Результат: exit 124; stack dump на 20 s показывает ожидание async generator fixture setup, aiosqlite worker и event loop selectors.select. До доказанного выполнения тела E4 теста дело не дошло. Причина зависания не установлена; это отдельный открытый вопрос окружения/async fixture. Зачесть E4 по этому прогону нельзя.

Сначала нужен защитный preflight: writable GOCACHE в изолированном каталоге, bounded Go probe, классификация environment acceptance failure без повторного запуска генератора. Затем диагностика источника env и async-fixture hang. Сейчас реализация этих исправлений не выполнялась.

## Найденные разрывы вертикального цикла

1. **Ложный done при конфликте.** `src/scheduler.py:211–234` и `src/registry/scheduler.py:179–198`: merge с `ok=False` оставляет `result.ok=True`; затем finish_step может завершить шаг успешно. Заказ завершается по состояниям шагов, без финального прогона интегрированного дерева.
2. **Опасный общий checkout.** `src/workspace.py:50–65` делает checkout/merge/checkout в исходном репозитории без блокировки по repo. Нужен отдельный integration worktree и сериализация интеграции. Текущий механизм нельзя разрешать для NexusBro параллельно с другой работой.
3. **Нет передачи кода по DAG.** `workspace.py:28–37` создаёт дерево от текущего HEAD исходного checkout, а результаты складываются в dirigent/integrate. Reviewer после coder может проверить старый dev. Входной SHA задачи и SHA результата зависимостей не закреплены.
4. **Reviewer не обязателен.** `planning.py:56–108` проверяет допустимые роли и machine checks, но не требует reviewer-узла, зависимости от coder или отдельной identity. `files_exist` и роль reviewer сами по себе не доказывают проверку результата другого агента.
5. **Два разных registry.** Публичный `src.registry.ExecutorRegistry` из core.py — select-only с `Selection.tag`; legacy registry.py — multi-launch с `Selection.executor`. E4 правильно уводит planned-заказы в select-only путь и оставляет reserve/retry/recovery у M1. Без этого legacy путь может выполнить несколько запусков на один M1 debit. Эти реализации нельзя механически подменять друг другом.
6. **Бюджет неполон.** M1 сохраняет debits атомарным условным INSERT, но launch_cost — оценка верхней границы, а не enforced backend cap. Planner и supervisor не включены в общий ledger. `supervisor.py:145–149` запускает opencode напрямую; none/invalid могут опрашиваться каждые 180 s без расходования лимита вмешательств. E1 can_launch проверяет общий остаток относительно reserve, не доказывает ограничение всех расходов заказа. Неизвестный расход нельзя объявлять нулём.
7. **Runtime replan отсутствует.** max_replans ≤2 у M1 ограничивает исправления предложения до утверждения. Supervisor разрешает retry_step/cancel_order/none, но не изменение DAG. Два вмешательства supervisor не эквивалентны двум перепланированиям.
8. **Вход не собран.** CLI submit (`cli.py:18–24`) обходит PlanningRequest и Planner.propose/approve. В PlanningRequest нет строгих permissions/allowed paths/operations. policy.py открывает почти весь /matrix; это не разрешение конкретного заказа и не защита NexusBro.
9. **Журнал попыток неполон.** Lease/transition/failover уже сохраняются, attempts растёт при lease. Lease не равен фактическому старту процесса. Нужны отдельные attempt start/end, identity, SHA, расход и причина каждого решения supervisor, включая none/refused.

Сохранять существующие Scheduler, SQLite recovery, state machine, M1 DAG validation и durable debits. Нового второго execution loop не требуется.

## Контракты следующего этапа

Это проект контрактов для утверждения, не изменение работающего сервиса.

- Вход GoalRequest: goal, DoD, total budget, finite call caps, permissions с canonical repo и allowed paths/operations. Production deploy, Telegram, proxy и системные сервисы запрещены для этого цикла; NexusBro исключён из испытаний.
- PlanRevision: version, parent revision, nodes, artifact dependencies, validation result, persistent runtime_replan_count ≤2. Уже выполненные результаты неизменяемы; перепланирование не сбрасывает attempts/debits.
- TaskInput/TaskResult: закреплённые base_sha, dependency SHAs, output_sha/artifact hashes, executor identity, attempt_id. Передача между узлами по SHA/артефактам, не через текущую ветку общего checkout.
- AttemptLedger: reserve до каждого платного вызова, затем actual_start/end, bounded timeout, usage либо unknown. Единый учёт worker/planner/reviewer/supervisor; глобальный потолок retry без вложенного multi-launch обхода. Неизмеримый backend допускается лишь с утверждённой обеспечиваемой верхней границей ресурса.
- ReviewResult: identity отличается от автора, проверяемый SHA совпадает с TaskResult, machine checks и решение сохранены. Интегратор принимает только проверенный SHA.
- IntegrationResult: отдельное дерево, repo lock, входные и выходной SHA; conflict/failed checks блокируют done. Финальная приёмка выполняется на точном интегрированном SHA; её receipt обязателен для order.done.

## Пакеты параллельной работы после утверждения

| Пакет | Границы файлов | Зависимости и результат |
| --- | --- | --- |
| A Контракты и вход | planning.py, новый contracts module, тесты планов | Сначала фиксирует схемы permissions/SHA/replan/review. До этого остальные пакеты не меняют общие модели. |
| B Ledger и бюджет | budget.py, отдельный ledger module; DB migration у одного владельца | После A; включает все платные вызовы, durable counters и recovery. |
| C Git интегратор | workspace.py, новый integration module, отдельные git fixtures | После A; отдельное дерево, pinned SHAs, repo lock, conflict→failed. Может идти параллельно с B/D. |
| D Независимая проверка | новый review module, checkers, отдельные review tests | После A; читает SHA автора, фиксирует identity и receipt. Может идти параллельно с B/C. |
| E Supervisor | supervisor.py, отдельные replanning tests | После A/B; bounded runtime DAG patch, причины всех решений, budget admission. |
| F Wiring и E4 | scheduler.py, registry/scheduler.py, E4 test/doc | Один интегратор владеет обоими scheduler-файлами; сохраняет текущий diff E4, подключает B/C/D/E. |
| G Независимые испытания | отдельный integration harness и fixtures | Отдельный Codex-агент; после контрактов может готовить стенд параллельно, запуск итоговой приёмки после F. |

Конфликты: E4 уже меняет оба scheduler-файла; не давать их нескольким исполнителям. Models/DB migration также имеют одного владельца. A/F могут быть последовательными задачами одного интегратора; B/C/D — независимые пакеты. Одновременно не более двух исполняющих задач на DE-1. В этом аудите использован один дополнительный Codex-агент для read-only проверки контрактов, без заказов и изменения файлов.

## Порядок интеграции и машинная приёмка

1. После утверждения сохранить diff E4 отдельным коммитом/патчем, зафиксировать новый baseline с учётом стороннего 188aa12; не cherry-pick уже включённые M1/M2. Защитить merge failure, общий checkout и environment preflight прежде расширения автономии.
2. Утвердить A; выполнить B/C/D параллельно в отдельных деревьях. Внести E, затем F одним владельцем scheduler. Не менять живые статусы прежних заказов на done без отдельного reconciliation receipt с SHA и результатами приёмки.
3. Локальное интеграционное испытание в одноразовом git-репозитории: текстовая цель → реальный planner adapter (сначала детерминированный subprocess fixture) → DAG из двух независимых workers, review, integrate, final acceptance. Два разных subprocess должны иметь пересекающиеся интервалы actual_start/end; reviewer проверяет SHA другого исполнителя. Специальный первый сбой обрабатывается без владельца; SQLite закрывается/открывается посередине для проверки восстановления.
4. Отрицательные сценарии: exhausted budget до запуска, конфликт merge, неверный SHA review, duplicate retry после рестарта, третье replan, timeout и неизвестный usage. Во всех случаях запрещён ложный done; все решения и отказы сохраняются.
5. Затем отдельно утверждённое испытание настоящих CLI/LLM с заранее установленными total cap и finite calls. Mock barrier E4 не заменяет эту проверку: E4-тесты прямо запрещают subprocess/socket. Доказательства: DAG JSON, versioned policy, SQLite events/attempt ledger, интервалы двух исполнителей, review receipt, integration SHA и финальный acceptance receipt, итоговый расход с unknown отдельно.

Текущее состояние: **аудит завершён, реализация следующего этапа не начата; критерий автономного производства не выполнен**. Для следующего этапа нужны утверждение контрактов/пакетов и конкретный лимит живого испытания; сейчас новых заказов нет.

## Передача отчёта

Доступный MCP Dirigent не содержит инструмента отправки отчёта. Проверен также dispatcher в src/mcp_server.py: в его registry нет report/post_notice. Локальный src/notice.py позволяет post в живую SQLite, но не адресует получателя и не подтверждает доставку; этот путь не использован. Применён разрешённый fallback: данный файл сохранён в репозитории, путь передан владельцу в ответе. Никакие сообщения в Telegram не отправлялись. Каноническая внешняя память вне разрешённых для записи путей в этой сессии; её индекс не изменялся.
