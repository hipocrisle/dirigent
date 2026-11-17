# Executor registry / capability routing (минимальный модуль)

Каталог: `src/registry/`. Задача: выбирать исполнителя (Codex / OpenCode-DeepSeek /
Gemini) по роли задачи, фактическому состоянию (`probe()`) и бюджету запусков,
с честными причинами выбора и ограниченным failover.

Ключевой принцип: **ничего не выдумывать**. Если факта про квоту/стоимость нет —
`unknown`, и `unknown` никогда не трактуется как «доступно». Никаких платных
проб: probe читает только локальные факты (бинарь, файл авторизации, журнал
последнего запуска).

## Состав

| файл | что делает |
|---|---|
| `probe.py` | `ProbeResult` (installed/authenticated/available/quota_state/last_error/...), `QuotaState`, восстановление последнего факта из журнала |
| `capabilities.py` | роли (`architecture/review/coding/cheap_test/general`) и карта наблюдаемых возможностей; порядок предпочтений |
| `registry.py` | `ExecutorRegistry`: `probe()/probe_all()/select()/launch()`, бюджет запусков, idempotency, параллелизм, audit |
| `audit.py` | JSONL-журнал решений (`probe/select/launch/launch_result/exhausted`) |
| `gemini.py` | адаптер Gemini CLI (единственный новый адаптер; codex/opencode переиспользуются) |
| `scheduler.py` | `RegistrySchedulerMixin` + `RegistryScheduler` — включение реестра в ядро без переписывания |

## Интерфейс

```python
reg = ExecutorRegistry()                       # reuse src.executors.registry + gemini (если установлен)
probes = await reg.probe_all()                 # {tag: ProbeResult}
pr = await reg.probe("codex")                  # кэш probe_ttl=60s, force=True — мимо кэша

sel = await reg.select(Role.architecture,      # или строка "coding"
                       policy=None,            # "prefer:opencode" — жёсткое предпочтение
                       budget=3,               # сколько запусков шага допустимо всего (≤3)
                       capabilities=["code_edit"])
# sel.executor, sel.reason, sel.candidates{"codex": "почему отброшен", ...}

out = await reg.launch(step_spec, role=Role.coding, workdir=..., launch_key="ord:s1")
# out.ok, out.result (StepResult), out.attempts[(executor, result, reason)], out.deduplicated
```

`ProbeResult`: `installed`, `authenticated`, `available` (= installed AND
authenticated AND не auth-ошибка), `quota_state` (`unknown|ok|exhausted`),
`quota_reset_at` (None = unknown), `last_error_class/text/at`, `notes`.
`quota_used/quota_limit` — всегда None: источник их не сообщает, оценки не делаем.

## Правила маршрутизации (фактические, а не «примерно»)

| роль | порядок |
|---|---|
| `architecture`, `review` | codex → opencode → gemini |
| `coding` | opencode → codex (**gemini не может править файлы**) |
| `cheap_test` | gemini → opencode → codex (gemini только если probe.available) |
| `general` | codex → opencode → gemini |

- Сложная архитектура и ревью предпочитают Codex даже при доступном Gemini.
- «Дешёвые тесты» на Gemini — это **эвристика выбора окна**, не утверждение о цене:
  у Codex квотный режим, у OpenCode — prepaid usage; цифр не придумываем.
- Failover только для `quota/network/auth/timeout/resource`. `runtime/unknown` —
  терминально (это не «похоже на сеть»).
- Бюджет шага: **≤3 запуска** (`policy.MAX_STEP_LAUNCHES`), включая первый.
- Параллелизм: **≤2** одновременно (`policy.MAX_PARALLEL_EXECUTORS`), свой
  семафор реестра в дополнение к семафору ядра.
- Идемпотентность: `launch_key` (по умолчанию `id:type:prompt[:120]`, в
  планировщике — `oid:sid`). Повтор с тем же ключом = replay без запуска.
  `reg.forget(key)` — осознанный ручной ретрай.
- Консервативный квот-режим: `exhausted` + `reset_at` в будущем — исполнитель
  исключён; `exhausted` без `reset_at` — исключён безусловно; после истечения
  `reset_at` Codex снова допускается **только** если иначе бюджет исчерпается
  («последний шанс», в `reason` явно об этом сказано).

## Включение в планировщик (одна правка ядра)

В `src/scheduler.py` в `_run_inner` (после `spec = ...`) добавлено 4 строки:

```python
if getattr(self, "_is_registry_step", None) and self._is_registry_step(spec):
    return await self._run_via_registry(oid, sid, spec, repo)
```

Проверено, что эта ветка не срабатывает для базового `Scheduler` и для обычных
шагов с `type=mock.*`/`codex` — они идут прежним путём.

## Семантика ошибок в машине состояний (registry-шаг)

| итог запуска | состояние шага |
|---|---|
| успех | completed (с обычной приёмкой/мерджем/worktree) |
| quota + неизвестный reset | failed — неизвестная квота не оставляет шаг в вечном `waiting_quota` |
| quota + известный reset в будущем | учитывается в `probe`; шаг ждёт, пока ресета нет, а после допускается |
| `resource` (ни одного исполнителя) | waiting_resource, без запусков и расхода бюджета |
| network/timeout с исчерпанным бюджетом | failed после ≤3 попыток (или retry ядра по `retry.max_attempts`) |

Найдено при интеграции (см. «Незакрытые зависимости»): ядро ставит
`waiting_resource` из `ready`, а не из `running`; registry-путь обходит это
через `ready` (force) → `waiting_resource`.

## Аудит

Записи JSONL (по умолчанию `/matrix/dirigent/var/registry-audit.jsonl`,
переопределяется `DIRIGENT_REGISTRY_AUDIT`): `{at, kind, executor, role, reason,
order_id, step_id, attempt, data}`. Реконструкция: «первый select — codex
потому что role=architecture; второй — opencode, потому что quota исчерпана».
`data.probe` содержит полный probe, `data.is_run` — факт запуска для восстановления
квоты после рестарта демона.
