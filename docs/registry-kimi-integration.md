# Интеграция registry/capability routing — инструкция для Kimi

Модуль: `src/registry/` (новый каталог). Тесты: `tests/test_registry*.py`,
`tests/fakes/{registry_fakes.py,gemini-fake.sh}`. Документация: `docs/registry.md`.

Принцип: ничего не выдумываем (квота/стоимость — только факт, иначе `unknown`);
никаких платных проб; codex/opencode переиспользуются как есть, gemini —
единственный новый адаптер.

## Статус

- Тесты: `tests/test_registry.py`, `tests/test_registry_gemini.py`,
  `tests/test_registry_scheduler.py`, `tests/test_registry_probe_real.py` — см.
  прогон ниже.
- Единственная правка ядра: **4 строки** в `src/scheduler.py::_run_inner`
  (ветка для registry-шагов). Неактивна для базового `Scheduler` и обычных шагов.
- В `docs/registry.md` — интерфейс, правила, семантика ошибок.
- `TASKS.md`/`PLAN.md`/UI/Telegram/systemd/secrets/release — не трогались.

## Что сделать Kimi (по шагам)

1. **Прочитать `docs/registry.md`** (10 минут). Это контракт: роли, порядок
   предпочтений, failover-классы, бюджет ≤3, параллелизм ≤2, idempotency.

2. **Ничего не включать, пока не пройдут тесты против текущего dev:**

   ```bash
   cd /matrix/dirigent
   /matrix/dirigent/.venv/bin/python -m pytest -q tests/test_registry.py \
       tests/test_registry_gemini.py tests/test_registry_scheduler.py \
       tests/test_registry_probe_real.py
   ```

3. **Решить, включать ли реестр в демон.** Два варианта:

   a) **Без изменения кода демона** — заменить класс планировщика на
      `RegistryScheduler` в точке, где демон создаёт `Scheduler` (`src/cli.py`):

      ```python
      from src.registry import RegistryScheduler
      sched = RegistryScheduler(db, max_parallel=2)   # вместо Scheduler(db)
      ```

      Обычные шаги (`mock.*`, `codex`, `opencode`) идут прежним путём — ветка
      registry в `_run_inner` их не задевает (проверено тестом
      `test_plain_mock_step_still_uses_old_path`).

   b) **Использовать `RegistrySchedulerMixin`** — если у демона свой подкласс
      `Scheduler`: унаследовать `RegistrySchedulerMixin` + `Scheduler`.

   Затем шаги, которые должны маршрутизироваться, получать `type: registry`
   (+ `inputs.role`, `inputs.capabilities`, `inputs.budget`), либо явно
   `executor: registry` при любом `type`.

4. **Одним заказом убедиться на bench-pilot** (`/matrix/bench-pilot/repo`):

   ```yaml
   goal: registry smoke
   repo: /matrix/bench-pilot/repo
   steps:
     - id: s1
       type: registry
       inputs: {role: coding}
       prompt: "добавь Mul в bench"; acceptance: {checks: [{kind: go_test}]}
   ```

   Ожидаемо: в `var/registry-audit.jsonl` записи `probe → select(opencode) →
   launch(opencode) → launch_result`; worktree-ветка `dirigent/<ord>-s1`.

5. **Жёсткое предпочтение**: `inputs.policy: "prefer:codex"` — если исполнитель
   доступен, берётся он; иначе честный fallback с причиной в audit.

6. **Проверить, что `policy.py` не разошёлся**: реестр импортирует
   `MAX_STEP_LAUNCHES`/`MAX_PARALLEL_EXECUTORS` из `policy.py` и **не может**
   поднять лимит выше них (параметры конструктора зажимаются `min(...)`).

## Не трогать без отдельного согласования

- `src/models.py`, `states.py`, `db.py` — интерфейс registry их не меняет.
- `src/executors/*` — codex/opencode не правились.
- `TASKS.md`, `PLAN.md` — допишите статус сами, владения файлов за вами.
- Системные юниты, Telegram, UI, release — вне скоупа этой задачи.

## Известные ограничения (решения, а не баги)

- `GeminiExecutor` **не авторизован** на DE-1 по нашим фактам (см. ниже).
  Если Gemini понадобится — нужен один smoke `gemini -p "ok"` с разрешения
  владельца; после подтверждения авторизация автоматически увидится, но
  в любом случае Gemіни-адаптер добавляется в реестр только при наличии бинаря.
- `quota_used/quota_limit` всегда `None`: источник квоты (tier-2) не реализован.
  Не показывайте в UI «% квоты» из этого модуля.
- `RETRY`: реестр тратит бюджет ≤3 запусков **внутри одной попытки** шага;
  ядро может повторить попытку (`retry.max_attempts`) — суммарный счётчик
  остаётся ограниченным только политикой шага. Если нужен глобальный потолок
  на шаг — это изменение `db.finish_step`/`attempts`, решать вам.
- Встроенный кэш `launch_key` живёт в памяти процесса: после рестарта демона
  replay невозможен, но и «ложного» повторного запуска не произойдёт только
  потому, что шаг уже completed (состояние в БД).
