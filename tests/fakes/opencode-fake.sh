#!/usr/bin/env bash
# Фейк OpenCode CLI для тестов WP-2.
# СИНТЕТИЧЕСКИЕ тексты, для тестов классификации; не копия реальных строк.
# Использование: opencode-fake.sh run [flags] -- <prompt>; режим — OPENCODE_FAKE_MODE
#   success | auth | runtime | network | usage_nested | hang
set -u
MODE="${OPENCODE_FAKE_MODE:-success}"

case "$MODE" in
  success)
    echo 'Ответ: ок'
    echo '{"usage": {"tokens_in": 12, "tokens_out": 3, "usd": 0.0012}}'
    exit 0
    ;;
  usage_nested)
    echo 'Ответ: ок'
    echo 'tool output line'
    echo '{"message": {"usage": {"tokens_in": 9, "tokens_out": 1, "usd": 0.0004, "extra": "x"}}}'
    exit 0
    ;;
  auth)
    echo 'Error: Authentication Fails, Your api key: ****abcd is invalid' >&2
    exit 1
    ;;
  runtime)
    echo 'Error: model deepseek/deepseek-chat is not available for this provider' >&2
    exit 1
    ;;
  network)
    echo 'Error: fetch failed: connection refused' >&2
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
