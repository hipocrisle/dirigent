#!/bin/bash
# MCP-стек Дирижёра для ChatGPT-теста: dirigent API :9310 + MCP-шлюз :9320.
# Запускать ВНУТРИ tmux-сессии (DE-1: фон только tmux/systemd).
# Туннель — отдельная сессия: cloudflared tunnel --url http://127.0.0.1:9320
set -e
cd /matrix/dirigent
export DIRIGENT_MCP_TOKEN="$(cat /matrix/secrets/dirigent-mcp-token)"

echo "[stack] dirigent api на 127.0.0.1:9310"
.venv/bin/python -m src.cli api &
API_PID=$!
sleep 2

echo "[stack] MCP-шлюз на 127.0.0.1:9320 (bearer из /matrix/secrets/dirigent-mcp-token)"
.venv/bin/python src/mcp_server.py &
MCP_PID=$!

trap "kill $API_PID $MCP_PID 2>/dev/null; exit 0" TERM INT
wait
