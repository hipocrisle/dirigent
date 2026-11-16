#!/usr/bin/env bash
# Фейк Codex CLI для тестов WP-2.
# СИНТЕТИЧЕСКИЕ тексты, для тестов классификации; не копия реальных строк.
# Использование: codex-fake.sh MODE [prompt], где MODE:
#   success | quota | auth | network | silent | runtime | hang
set -u
# Режим: первый аргумент; если он `exec` (как настоящий CLI) — из CODEX_FAKE_MODE.
MODE="${1:-success}"
if [ "${MODE}" = "exec" ]; then MODE="${CODEX_FAKE_MODE:-success}"; fi

case "$MODE" in
  success)
    echo "Готово: задача выполнена."
    echo "tokens used: 1234"
    exit 0
    ;;
  quota)
    echo "ERROR: You've hit your usage limit. Upgrade to Plus or try again later." >&2
    echo "Your limit resets at 4:36 PM." >&2
    exit 1
    ;;
  auth)
    echo "error: authentication failed (401 Unauthorized). Try 'codex login' again." >&2
    exit 1
    ;;
  network)
    echo "error: failed sending request: connection reset by peer (ECONNRESET)" >&2
    exit 1
    ;;
  silent)
    exit 0
    ;;
  runtime)
    echo "error: unexpected tool failure while applying patch" >&2
    exit 1
    ;;
  hang)
    sleep 999
    ;;
  *)
    echo "fake: неизвестный режим $MODE" >&2
    exit 2
    ;;
esac
