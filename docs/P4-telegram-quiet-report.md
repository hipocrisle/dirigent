# Отчёт: Telegram production quiet-by-default (P4, ордер `ord_7591291f96d3`)

Дата: 18.11.2026. Для ревью Kimi. **Деплой НЕ выполнялся**, секреты/токены не
трогались, NexusBro/systemd/порты не затронуты. Второй бот не создавался —
использован существующий `/matrix/telegram-bot` (`bot.py` + `bot_dirigent.py`).

## 1. Причины инцидента 17.11 (по коду и по фактам журнала)

| Симптом | Причина в коде |
|---|---|
| Десятки сообщений/мин | `notify.MODE = "verbose"` (прод!); `pause/resume` слали TG; нет rate-limit/coalescing |
| Уведомления от 'test','t','kill-guard', bench-pilot | Фильтра тестовых заказов не было вообще |
| Дубли done/error | `_update_orders` вызывал `enqueue` каждый tick, а не только на переходе |
| Пустые approvals `([ ])`, TTL 1 мин | `_open_approval` слал всё подряд; TTL брался из spec как есть |
| Кнопки Разрешить/Отклонить не работали | `callback_data` слался `dgt\|apr\|…`; общий `callback_handler` бота (без фильтра) ловил их и на неизвестном префиксе не делал `answer`; в `bot_dirigent` `_post_json` не существовал, `is_admin` вызывался как F821 |

**Дополнительно найдено при разборе (новое к прежнему отчёту):**

1. **Живой канал флуда — sender демона.** В прод-БД
   `/matrix/dirigent/dirigent.db` таблица `notifications` = **2055 `sent`**
   (отправлено боевым токеном через 127.0.0.1:8081), при 0 `pending`. Старый
   код (в живом процессе) `_conn()` создаёт `notifications` в прод-БД без
   вопросов, и sender шлёт всё, что туда положил демон или **тестовый процесс
   на том же рабочем дереве** (в `tests/` не было изоляции журнала).
2. **Approvals с TTL 1 мин были МЁРТВЫМИ при создании.** `db.create_approval`
   ждёт TTL в секундах, а `_open_approval` передавал `spec.approval.timeout_seconds`
   (3600) — в журнал шло `expires_at = now + 3600`, и `Scheduler` тут же истекал
   апрув: `/approvals/open` пуст, approve/reject ничего не делал. Отсюда «кнопки
   не работают» в том числе на уровне API. В старом коде имелась мина: если бы
   кто-то передал `timeout_seconds` как минуты (1) — апрув истекал за секунду.
3. `src/api.py` на `approve/reject` возвращал только `{"ok": bool}`, где `ok`
   означал «переход шага удался», а не «решение применено» — из-за этого бот
   не мог отличить «просрочено» от «применено».
4. Повторное решение (двойной клик / CLI и кнопка одновременно) обрабатывалось
   как «не удалось», не атомарно.

## 2. Поведение после правок (quiet-by-default)

Владельцу уходят **только**:
* реальный `approval.requested` (апрув существует, открыт, не истёк и у него
  есть что решать — см. п.4);
* `order.error` / `step.retry_refused` — критические ошибки после исчерпания
  попыток;
* `order.done` — завершение **важного реального** заказа.

`order.paused` / `order.resumed` и прочие переходы — только в `events` (UI/аудит),
в Telegram не уходят. `MODE = "quiet"` — константа прода.

**Классификация заказа** — по явным метаданным, НЕ по вхождению «test» в цель:
* `repo` под `/matrix/bench-pilot` → `test`; `/matrix/dirigent-selfcheck` → `service`;
* тип шага `mock.*` / `bench.*` / `selfcheck.*` → `test`;
* точное имя служебной проверки (`kill-guard`, `self-check`, `healthcheck`) или
  `constraints.test` / `constraints.self_check` / `constraints.in_scope` → `test`/`service`;
* заказа нет в журнале → `unknown` → fail-closed (молчим).

Так, исторический `ord_b6d328a8629e "allowlist self-check"` (шаг `mock.ok`)
теперь `test` — молчит; `ord_4c09989c73e7` (bench-pilot, `mock.ok`) — `test`.

## 3. Дедуп, coalescing, rate-limit

* `UNIQUE(dedup_key)` (как было) + явная проверка `event_id` (kind+order+step).
* **Новая** проверка order+transition: `event_data_json` нормализуется
  (`notify._signature`, отбрасываются `ts/id/seq/approval`) и хранится в новой
  колонке `notifications.event_json`; тот же переход того же заказа в окне
  10 мин → второе уведомление не создаётся, событие остаётся в аудите.
* coalescing через атомарный claim (`INSERT OR IGNORE` по `merge=<kind>`).
* rate-limit 5/мин **только для не-критических**: откладывает (`next_retry`),
  не теряет. Критические (апрувы, ошибки) идут первыми и лимитом не режутся.
* **Предохранитель живого потока**: `PRODUCTION_MODE` фиксируется при импорте;
  `sender_loop` запоминает его на старте; `send_pending_once` не отправляет
  ничего вне прод-режима без явного флага `_allow_nonproduction_send` (его
  ставят только тестовые фикстуры). В тестах `_bot_token()` ещё и всегда
  `None` — два независимых барьера.
* **Последний рубеж в sender**: перед отправкой строка перепроверяется на
  классификацию (`is_user_facing`), неактуальные/тестовые → `skipped`.
* Демон не блокируется: sender живёт в `asyncio.to_thread`, `busy_timeout=5000`,
  все ошибки — soft-fail с `log.exception`.

## 4. Апрувы: TTL и «нечего решать»

* `effective_approval_ttl`: заявленный TTL берётся, только если `≥ 300 с`,
  иначе часовой дефолт (молчание ≠ согласие; TTL явно показан в карточке).
  TTL **вычисляется в момент отправки** из журнала — карточка, пролежавшая в
  очереди, не врёт про срок.
* `is_meaningful_approval`: пустые артефакты `[]` / `{}` без действий →
  апрув пишется в журнал для аудита (`approval.not_notified`), владельцу не шлётся.
* `enqueue` дополнительно: апрув должен существовать и быть `open`; при
  нереальном/истёкшем/решённом апруве — отказ.

## 5. Кнопки: callback_data, handler, auth, ack, однократность

* `callback_data = dgt:apr:<approval_id>:ok|no` (≤ 64 байт); для ID, который не
  влезает, кнопок нет вовсе, и карточка не отправляется (`approval_keyboard_markup`
  → `[]`). `notify.approval_keyboard()` оставлен для совместимости.
* `bot_dirigent.parse_callback` понимает `dgt:apr:…` и старый `dgt|apr|…`.
* **ack всегда**: callback подтверждается до любого выхода (мусор, не-админ,
  офлайн API); `_safe_answer` повторяет `q.answer` до 3 раз.
* **auth по Telegram user ID**: `bot.is_admin` → фолбэк `allowed_users.json` по
  `str(uid)`, `roles`/`role`; username не используется. Не-админ: API не
  вызывается, ответ «⛔ Admin only».
* **Однократность — на API**: `actions._claim_approval` атомарно переводит
  `status='open'` → `approved/rejected` (`UPDATE … WHERE status='open'`);
  проигравший гонку получает текущее состояние.
* **Ответ API**: `{ok, state, order_id, step_id}`; `state` ∈ `approved |
  rejected | expired | timeout | missing(404)`. Бот переводит это в понятный
  текст: «⌛ Срок решения истёк — решение не применено, повтор не применится»,
  «ℹ️ Уже разрешён ранее — повтор не применён» и т.п.
* После решения (из кнопки, `/approve` или CLI) API гасит кнопки уже
  доставленных карточек (`notify.acknowledge_approval_cards`), pending-строки
  помечаются `decided`.

Токены для этого не нужны: всё идёт через существующий локальный Bot API
(127.0.0.1:8081) и существующий `bot.py` (маршрутизация `dgt:*` отдельным
`CallbackQueryHandler`, общий обработчик получил negative-lookahead).

## 6. Изменённые файлы

### Дирижёр (`/matrix/dirigent`)
| Файл | Что | Источник |
|---|---|---|
| `src/notify.py` | quiet-тиры, классификация, dedup/coalesce/rate-limit, TTL, дедуп по event+data_json, `PRODUCTION_MODE`, `approval_keyboard_markup`, `acknowledge_approval_cards`, проверка классификации в sender, удалён дубль `approval_outcome_message` | переработка (часть ранее — в этом worktree) |
| `src/scheduler.py` | `_open_approval` — TTL через `effective_approval_ttl`, пустой результат не шлётся; `_update_orders` — уведомления только на реальном переходе; `_notify_error` — только исчерпавшие попытки; `event_data_json` для dedup | переработка |
| `src/actions.py` | `_claim_approval` (атомарный однократный approve/reject); `pause/resume` без TG (только аудит) | **новое** — claim |
| `src/api.py` | `{ok, state, order_id, step_id}` на approve/reject, гашение карточек, `state` отделён от `ok` | **новое** — состояния/ack |
| `docs/P2-telegram-spec.md` | поправка P4 к спецификации | ранее в worktree |
| `docs/P4-telegram-quiet-report.md` | этот отчёт | обновлён |

### Бот (`/matrix/telegram-bot`)
| Файл | Что |
|---|---|
| `bot_dirigent.py` (не в git) | `callback_dirigent`: ack всегда + повтор, права по user ID, понятные ответы по TTL/повтору, карточка гасится при окончательном состоянии; `is_admin` — строго по ID; `outcome_text` — понятнее для просрочки |
| `bot.py` (незакоммиченная правка в репо бота) | `CallbackQueryHandler(callback_handler, pattern=r"^(?!dgt[:|])")`, чтобы общий обработчик не перехватывал `dgt:*` |

## 7. Тесты (pytest, `tests/`)

Полный прогон: **211 passed, 2 failed**. Оба падения — `tests/test_checkers.py`
(`test_go_test_ok`, `test_run_acceptance_chain`) и **только из-за отсутствия
`GOCACHE` в окружении**: с `GOCACHE=/tmp/opencode/gocache` те же тесты —
`12 passed`. К изменениям отношения не имеют.

Новые/дополненные регрессии:

* `tests/notify_fixtures.py` — реальная схема журнала, фейковый TG, `_allow_nonproduction_send`.
* `tests/conftest.py` — autouse-изоляция журнала per-test; **autouse `_no_real_telegram`** (token всегда `None`).
* `tests/test_notify.py` (23) — quiet-дефолт; тесты/служебные/bench-pilot молчат; голова «test» в цели — не критерий; **флуд 30 тестовых событий → 0 сообщений**; coalescing 30 переходов → 1; rate-limit не теряет апрувы; backoff; 4xx не ретраится; реальные error/done доставляются; кнопки ≤64 байт; пустые/решённые/просроченные апрувы не шлются; TTL «1 мин» → 3600; TTL в карточке из журнала; `mark_decided`; `recover_after_restart`.
* `tests/test_notify_integration.py` (9) — сквозь `Scheduler.tick()`: mock/bench → тишина; реальный апрув с артефактами → карточка; `([])` → только аудит; TTL ≥ 300; pause/resume — только аудит; реальный done — **ровно одно** сообщение; ошибка после исчерпания попыток — одно ❌ без сырого `{'failed':…}`; отменённый заказ — не ошибка.
* `tests/test_bot_dirigent.py` (24) — `callback_data` из outbox разбираются; старый `|`-формат; мусор; approve/reject → нужный эндпоинт **и ack**; просрочка/повтор → alert, кнопки сняты; офлайн → ack+ошибка; **ack повторяется, если первый упал**; не-админ не трогает API, но ack; права по ID, не username; фолбэк `allowed_users.json`; `/approve` для timeout; negative-lookahead в `bot.py`.
* **`tests/test_telegram_incident_regressions.py` (12, новый)** — sender молчит вне прод-режима (в т.ч. `sender_loop`); в прод-режиме тестовый заказ не уходит; повтор одного перехода (`order+transition`) → одно сообщение; **старые pending-строки из outbox не шлются**; 10 апрувов + 10 done → все апрувы доставлены, done придержаны; API: однократность, `expired`/`missing`; **callback_data из настоящей карточки outbox парсится ботом**; чужие/мусорные callback отклоняются; регэксп регистрации принимает все кнопки.

## 8. Что не сделано / пробелы для Kimi

1. **Не деплоил и не перезапускал демон.** Живой процесс продолжает работать со
   старым кодом, пока его не перезапустят после ревью. Это единственное место,
   где правки ещё не действуют.
2. **`bot_dirigent.py` не в git** (репозиторий бота: `bot.py` изменён, но не
   закоммичен; `bot_dirigent.py` untracked). Чужой незатреканный файл я не
   коммитил. Нужен коммит в репозитории бота перед деплоем.
3. **Реальная доставка кнопок не проверена вживую** — тесты используют фейковый
   Bot API и фейковый `q.answer`; токен и 127.0.0.1:8081 не трогались. После
   перезапуска стоит один раз нажать кнопку на настоящем апруве.
4. **«Важность» `order.done`** — эвристика (не-mock шаг / `constraints.important`
   / priority ≥ 2). Порог и правило стоит утвердить.
5. **TTL < 300 с принудительно повышается до часа** — сознательное отступление
   от spec ради живых кнопок; нужно согласование (если нужен строгий короткий
   TTL, менять `effective_approval_ttl`).
6. **Прод-БД**: 2055 исторических строк `notifications` в статусе `sent` — не
   чистил (ничего не отправляется, но их можно архивировать отдельной задачей).
7. **Проверка прав** дублируется в `bot.py` (`allowed_users.json`) и в
   `bot_dirigent.py` (фолбэк) — если файл станет недоступен/битым, фолбэк
   закроет доступ (fail-closed), но покажет «Admin only» живому админу.
