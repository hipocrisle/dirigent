# WP-2: Адаптеры исполнителей codex + opencode

Ты — исполнитель пакета WP-2 проекта «Дирижёр» (/matrix/dirigent).
Прочитай перед работой: PLAN.md (решения AD-1…AD-12), TASKS.md (владение файлами),
src/executors/base.py (ИНТЕРФЕЙС, заморожен — НЕ менять), src/models.py (StepResult,
ErrorClass), src/executors/mock.py (образец адаптера).

## Жёсткие рамки
- Создаёшь ТОЛЬКО эти файлы: src/executors/codex.py, src/executors/opencode.py,
  tests/fakes/codex-fake.sh, tests/fakes/opencode-fake.sh, tests/test_executors.py.
  Ничего больше не трогаешь (никаких правок base.py, mock.py, ядра).
- Каждый файл ≤ 250 строк. Комментарии только по делу. Всё по-русски, включая
  docstrings и сообщения об ошибках.
- Реальную квоту Codex НЕ расходуешь НИКАК: codex.py проверяется только фейком.
  Реальный smoke — только opencode (1 запрос, ~1 цент, разрешён).

## Задание

### 1. src/executors/codex.py — class CodexExecutor, tag="codex"
- run(step, workdir): subprocess `codex exec --full-auto <prompt>` (prompt из
  step.prompt, передаётся как argv, без shell=True), cwd=workdir или cwd не задан,
  env унаследованный + timeout=step.timeout_seconds, capture stdout/stderr.
- classify(stderr, rc) — ОТДЕЛЬНАЯ чистая функция (module-level), размечает:
  quota: stderr содержит "usage limit" или "rate limit" (case-insensitive);
         если там есть "resets at HH:MM" или "resets <время>" — вернуть также
         quota_reset_at (unix-time сегодня; парсер времени — отдельная функция
         parse_reset_time(str) -> float|None, тестируется отдельно на 5+ примерах);
  auth: "authentication" | "unauthorized" | "login required";
  timeout: rc==124 (или наш собственный TimeoutExpired -> timeout);
  network: "connection" | "econnreset" | "timed out" | "dns";
  rc==0 и пустой stdout -> runtime ("молчаливый успех недопустим");
  ничего не подошло -> unknown. НИЧЕГО не угадывать: unknown лучше ложного класса.
- available(): НЕ вызывает сеть и НЕ жжёт квоту: проверяет только наличие бинаря
  `codex` (shutil.which) и файла ~/.codex/auth.json. Это осознанное ограничение
  tier-1 (см. AD-4) — напиши его в docstring.
- status()/cancel(): для MVP возвращают None / no-op (внешний процесс умирает
  вместе с run; доработка с run_id — отдельный пакет).

### 2. src/executors/opencode.py — class OpencodeExecutor, tag="opencode"
- run(step, workdir): subprocess `opencode run --model deepseek/deepseek-chat --prompt <prompt>`…
  (формат: opencode run [--model m] "текст" — сверь фактический CLI через
  `opencode run --help`, адаптируй), cwd=workdir, timeout=step.timeout_seconds.
- usage: из stdout возьми последнюю JSON-строку если она есть
  ({"usage": {...}}) — opencode иногда так пишет; если нет — usage={}.
  Фактические поля: tokens_in, tokens_out, usd (float, не угадывать).
- classify(stderr, rc): auth ("Authentication Fails"), network/timeout/runtime
  — аналогично codex, quota-класс для DeepSeek НЕ используем (API у нас prepaid;
  квота Codex — отдельная история).
- available(): shutil.which("opencode") AND существует
  /root/.config/opencode/opencode.json.

### 3. tests/fakes/*.sh — заготовленные сценарии (НЕ сетевые)
codex-fake.sh MODE: success | quota (печатает типичный текст лимита с "resets at")
| auth | network | hang (sleep 999). opencode-fake.sh MODE: success (с JSON usage
в конце) | auth | runtime. Тексты ошибок бери реалистичными, но помечай в шапке
файла: «СИНТЕТИЧЕСКИЕ, для тестов классификации; не копия реальных строк».

### 4. tests/test_executors.py
- Параметризованный тест классификации: каждый MODE фейка -> ожидаемый ErrorClass
  (6+ кейсов на codex, 3+ на opencode).
- parse_reset_time: 5+ примеров форматов ("resets at 4:36 PM", "resets 16:36",
  "resets in 2 hours", неразбираемое -> None).
- available(): True на этой машине для opencode (файл конфига есть), codex — True
  только если бинарь+auth.json есть; тест должен быть skipped, если нет (не падать).
- run() против фейков через monkeypatch shutil.which/<путь>: success ок,
  quota -> StepResult(ok=False, error_class=quota, quota_reset_at float|None),
  hang -> timeout.
- Один реальный smoke opencode (маркер pytest.mark.real): `prompt="Ответь одним
  словом: ок"` — проверить ok==True. Запускать ВРУЧНЮЮ (не в CI-прогоне):
  pytest -m real.

## DoD (машинно проверяемо)
cd /matrix/dirigent && .venv/bin/python -m pytest tests/test_executors.py -q
→ все зелёные (кроме skipped). Имена тестов отражают сценарий.

## Отчёт (последним сообщением)
1) что сделано; 2) список файлов; 3) вывод pytest целиком; 4) что НЕ сделано/не
получилось; 5) замечания по интерфейсу base.py (если есть) — ТОЛЬКО как
наблюдение, не менять самому.

Начинай. Вопросы, блокирующие работу — сразу в ответ; остальное решай сам.
