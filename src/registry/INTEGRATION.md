# Registry / capability routing — интеграционная инструкция для Kimi

Минимальный модуль выбора исполнителя по фактам, без внешних платных API.
Код: `src/registry/`. Тесты: `tests/test_registry*.py`. `scheduler.py` НЕ
изменён (интеграция — отдельным шагом, см. «Что сделать Kimi»).

## Контракт

```python
from src.registry import ExecutorRegistry, Role, Budget, SelectionStatus

reg = ExecutorRegistry()                 # проберы по умолчанию: codex/opencode/gemini
probes = await reg.probe()               # dict[tag, ProbeReport]
# ProbeReport: installed, authorized, available, offline, quota_state, last_error, status

sel = await reg.select(
    role=Role.coding,                    # Role | "coding"|"architecture"|"review"|"simple_test"|"generic"
    policy={"exclude": [], "preference": None, "force_tag": None},
    budget=Budget(max_launches=3),       # клампится к policy.MAX_STEP_LAUNCHES (=3)
    capabilities={"code", "tests"},      # code/tests/cheap/architecture/review/long_context
    key=f"{oid}/{sid}",                  # идемпотентность шага
)
# sel.status: selected | none | exhausted | offline
# sel.tag: выбранный исполнитель (None при none/exhausted/offline)
# sel.reason: «почему» для аудита; sel.rejected: [{tag, why}]
```

### Факты (probe)
- `installed` — бинарь через `shutil.which`.
- `authorized` — локальный файл креденшала (`~/.codex/auth.json`,
  `~/.config/opencode/opencode.json`, `~/.gemini/gemini-credentials.json`).
- `available` — installed ∧ authorized ∧ не offline.
- `offline` — явный флаг (`offline=True` или env `DIRIGENT_REGISTRY_OFFLINE=1`).
- `quota_state` — `known_ok | known_exhausted | unknown | n/a`. **unknown ≠ available**:
  `ProbeReport.usable` = available ∧ quota ≠ known_exhausted.
- `last_error` — текст ошибки из пробы/адаптера (если был).

Сети и платных API нет. Codex-квота не прощупывается (tier-1, PLAN AD-4).

### Политика по ролям (`policy.ROLE_PREFERENCE`)
| Роль | Порядок |
|---|---|
| architecture | codex → opencode → gemini |
| review | codex → opencode → gemini |
| coding | opencode → codex → gemini |
| simple_test | gemini → opencode → codex |
| generic | opencode → codex → gemini |

Gemini заявляет только `tests, cheap` (capability). Для coding/architecture/review
он не выбирается, если запрошена capability `code`/`architecture`/`review`.

### Failover и лимиты
- `reg.note_quota(tag, reset_at=..)` / `reg.note_network(tag)` — адаптер
  сообщает факт, исполнитель уходит в карантин; следующий `select()` выбирает
  другого. Хелпер: `src/registry/bridge.py::failover_decision(reg, key, tag, result)`
  (срабатывает только на quota/network, не на runtime/unknown).
- `Budget.max_launches` жёстко ≤ `MAX_LAUNCHES=3`; исчерпание → `status=exhausted`.
  Pin не обходит лимит.
- Параллелизм: `reg.acquire_slot()/release_slot()/inflight/free_slots()`,
  максимум `MAX_PARALLEL=2` (совпадает с `policy.MAX_PARALLEL_EXECUTORS`).

### Идемпотентность
Тот же `key` + неизменные условия → тот же `tag` (`reason="idempotent: pinned"`).
Смена policy/capabilities, quota/network-карантин или exhausted снимают pin.

### Аудит
`reg.audit_log` (последний — `reg.last_audit(key)`), `RoutingAudit.as_dict()`
содержит `selection` (кто/почему) и `probes` (факты всех исполнителей на момент).

## Что сделать Kimi (интеграция в scheduler.py — по желанию, не обязательно)

Модуль самодостаточен и тестируется отдельно. Чтобы включить в ядро:

1. В `Scheduler.__init__`: `self.registry = ExecutorRegistry()` (или передать
   готовый через параметр для тестов).
2. В `_run_inner` после получения `spec` (шаг с тегом codex/opencode/gemini):
   - определить `role` шага (например из `spec.inputs.get("role")`, по
     умолчанию `generic`);
   - `sel = await self.registry.select(role, budget=Budget(), capabilities=read_capabilities(spec.inputs.get("capabilities")), key=f"{oid}/{sid}")`;
   - если `sel.status != selected` → `waiting_resource` + `db.event("step.routing", ...)` с `sel.as_audit()`;
   - иначе `tag = sel.tag`, после фактического старта `self.registry.note_launch(...)`.
3. После `ex.run()` (перед `finish_step`) — `failover_decision(...)`; на True
   перевести шаг в `retry_scheduled` (а не сразу `waiting_quota`), чтобы
   следующий тик выбрал другого исполнителя.
4. Аудит в БД: `await self.db.event("step.routing", oid, sid, sel.as_audit())`
   и/или `registry.last_audit().as_dict()`.

Существующий `test_quota_failover` (tests/test_p1.py) продолжает работать:
registry только добавляет маршрутизацию, не ломая `waiting_quota`-сценарий.

## Незакрытые зависимости
- **Роль шага**: у `StepSpec` нет поля `role`; нужно решить, брать из `inputs`
  или добавить в модель (владелец models.py — Kimi).
- **Аудит в БД**: сейчас `audit_log` в памяти; запись в `events` — за
  планировщиком (см. п.4).
- **Codex tier-2 quota probe** (WP-8) не реализован: `quota_state` Codex =
  unknown до явного `note_quota` от адаптера.
- **Gemini-адаптер**: probe есть, но `Executor`-адаптера для запуска Gemini
  нет — пока только маршрутизация; для реальных запусков нужен адаптер
  (вне scope этого пакета).
- **`CODEX_FAKE_MODE`/`--approve-for-me`**: живой смоук Codex по квоте всё ещё
  pending (см. TASKS.md).
