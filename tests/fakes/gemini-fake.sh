#!/usr/bin/env bash
# Фейк Gemini CLI для тестов registry. СИНТЕТИЧЕСКИЕ строки, не копия вывода CLI.
# Использование: gemini-fake.sh -p <prompt> -o text; режим — GEMINI_FAKE_MODE:
#   success | quota | auth | network | silent | hang
# GEMINI_FAKE_ARGV — файл, куда построчно записать argv.
set -u
MODE="${GEMINI_FAKE_MODE:-success}"
if [ -n "${GEMINI_FAKE_ARGV:-}" ]; then printf '%s\n' "$@" > "$GEMINI_FAKE_ARGV"; fi
case "$MODE" in
  success) echo "Ответ: ок"; exit 0 ;;
  quota) echo "Error: ResourceExhausted: 429 quota exceeded for project" >&2; exit 1 ;;
  auth) echo "API key not valid. Please pass a valid API key." >&2; exit 1 ;;
  network) echo "Network error: getaddrinfo ENOTFOUND generativelanguage" >&2; exit 1 ;;
  silent) exit 0 ;;
  hang) sleep 999 ;;
  *) echo "fake: неизвестный режим $MODE" >&2; exit 2 ;;
esac
