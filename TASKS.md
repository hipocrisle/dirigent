# Дирижёр — пакеты работ (WP). Статусы: todo | progress | review | done

Правила для всех исполнителей:
- Работаем ТОЛЬКО в /matrix/dirigent (кроме явно указанного).
- Никаких глобальных правок, ничего боевого на DE-1, секреты только через vault.
- Каждый пакет: маленькие файлы, тесты рядом, commit по смыслу в ветку `dev` репо dirigent (git init при первом пакете).
- Формат отчёта по пакету: что сделано / файлы / как проверял / что не работает.
- DoD пакета проверяется machine-способом (тесты запускаются — зелёные).

## Требования круга 4 (ChatGPT, принято к исполнению)
- Квота Codex (WP-2): пишет DeepSeek, НО независимое ревью Kimi обязательно до merge. Тесты на заготовленных выводах CLI, реальную квоту не жечь. Различение quota/auth/network/timeout, reset-time, отсутствие бесконечных ретраев, деградация при смене формата ответов, отключаемый probe.
- TUI (WP-6): только после вертикального среза. Порядок: ядро+мок → реальные CLI → очередь/квота/восстановление → Telegram → TUI → медиа. API проектируем TUI-ready сразу.
- Метрики достоверные: факт vs оценка раздельно; квота Codex, если не получить достоверно — показывать unknown/estimated, без иллюзии точности.
- Параллельность: владение файлами закреплено (ниже), общие интерфейсы до реализации, критические модули — ревью Kimi, интеграция и приёмка — Kimi.

## Владение файлами (кто правит что)
- Kimi (интегратор): src/models.py, states.py, db.py, dag.py, scheduler.py, brain/*, checkers/*, api.py, cli.py, tests/ (кроме fakes), TASKS.md/PLAN.md, requirements.txt
- DeepSeek: src/executors/codex.py, src/executors/opencode.py, tests/fakes/*, src/tui.py (после ревью адаптеров)
- Общее (правит только Kimi): src/executors/base.py — интерфейс исполнителей, заморожен до WP-2
- Gemini: examples/*, docs/*
- Никаких правок чужих файлов; общие изменения — через TASKS.md → Kimi.

## WP-1 [Kimi] Ядро: схема + машина состояний + очередь (фаза 1 спеки)
Файлы: src/db.py (SQLite WAL, миграции), src/states.py (переходы pending→ready→running→…→completed/failed, таблица переходов явная), src/scheduler.py (lease+heartbeat+semaphore=2+recovery), src/models.py (заказ/шаг, валидация pydantic или dataclasses+jsonschema), src/dag.py (топосорт, циклы).
DoD: `pytest tests/test_core.py` — зелёные; сценарий «restart посреди running» восстанавливается (тест C ядра).
Статус: ✅ done (14 тестов + смоук демона)

## WP-3 [Kimi] Acceptance checkers — ✅ done
checkers: go_test, diff_apply (git apply --check/patch на копии), artifact (ffprobe-длительность). Влито в scheduler: acceptance-fail = runtime.

## WP-2 [DeepSeek] Адаптеры codex + opencode — ✅ done (ревью Kimi: ПРИНЯТО)
Файлы: src/executors/codex.py, src/executors/opencode.py, tests/fakes/*, tests/test_executors.py (+test_real_smoke.py, отклонение от списка — не критично). 64/64 тестов. 4м48с, 308 т/с.
Ключевое: `--full-auto` в codex-cli 0.167.2 УДАЛЁН — DeepSeek сверил с реальным CLI и поставил `--approve-for-me` с явной оговоркой «не проверено на живой квоте» → follow-up: проверить флаг на первом реальном прогоне. usage-парсинг opencode: только факт из 3 известных JSON-форм, кэш-токены не выдумываем (честные метрики).
Ревью Kimi (обязат. по треб. ChatGPT №1): классификация честная (unknown вместо угадайки), порядок маркеров ок, parse_reset_time 6 форматов, таймаут-путь закрывает транспорт asyncio. ПРИНЯТО без правок. Наблюдения DeepSeek учтены: run_id-задел и «молчаливый успех=runtime» зафиксирован в base.py.

## WP-3 [Kimi] Acceptance checkers
Файлы: src/checkers/diff_apply.py (применение diff к копии worktree), src/checkers/go_test.py, src/checkers/lint.py, src/checkers/artifact_meta.py (mp4: длительность/разрешение через ffprobe; png: размер; json: схема).
DoD: checkers отрабатывают на bench-pilot (S1-задача: добавить Mul — из CHECKLIST.md бенча).
Статус: todo

## WP-4 [Kimi] Telegram-слой
Файлы: src/api.py (127.0.0.1:9310, endpoints: orders list/get/approve/pause/resume/cancel/budget, stream логов), правка /matrix/telegram-bot/bot.py (только добавление CommandHandler'ов: /orders /order /approve /dpause /dresume /dcancel /dbudget — тонкий прокси на 9310, ~80 строк, в отдельном файле bot_dirigent.py чтобы не мусорить), tests/test_api.py.
DoD: через curl с localhost все endpoints отвечают; сценарий «апрув неблокирующий» — тест G ядра.
Статус: todo

## WP-5 [Gemini] Фикстуры и примеры
Файлы: examples/order_video25.yaml (заказ «25-сек ролик» полностью по схеме), examples/order_refactor.yaml, examples/steps/*.yaml, docs/cheatsheet.md (как ставить заказ из TG простым текстом).
DoD: YAML валидируются схемой из WP-1 (CI-скрипт validate_examples.sh).
Статус: todo

## WP-6 [DeepSeek, после WP-2] TUI-дашборд
Файлы: src/tui.py (textual): слева заказы, справа дерево шагов, цвета состояний, зависшие мигают; клавиши r/a/k/l/s; шапка: $ по исполнителям (из usage-записей БД), % квоты Codex (заглушка до WP-8), brain-контекст.
DoD: запускается в tmux, виден заказ из теста A, ручной r на failed-шаге перезапускает его.
Статус: todo

## WP-7 [совместно] Пилот на bench-pilot
Сценарии тестов B (квота — мок), D (ошибка исполнителя), F (TG), G (согласование), H (бюджет), I (идемпотентность) из спеки §16. Замеры в PROTOCOL.md: время, токены, пинги хозяина, RAM/CPU.
Статус: todo

## WP-8 [Kimi, после пилота] Tier-2 quota probe
Порт read-only логики из Paperclip quota.ts (MIT, атрибуция) за флагом CODEX_QUOTA_PROBE=1.
Статус: todo

## Зависимости
WP-1 → всё. WP-2 ∥ WP-3. WP-4 после WP-1. WP-5 ∥ всё. WP-6 после WP-2. WP-7 после WP-2+WP-3+WP-4. WP-8 после WP-7.

## Журнал
- 16.11: пакеты созданы (Kimi), Фаза 0 в PLAN.md. Ждём «погнали» после круга 3 с ChatGPT.
