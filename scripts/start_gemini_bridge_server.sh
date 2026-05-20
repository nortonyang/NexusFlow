#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${GEMINI_BRIDGE_PID_FILE:-$ROOT_DIR/.gemini-bridge/gemini_bridge_server.pid}"
LOG_DIR="${GEMINI_BRIDGE_LOG_DIR:-$ROOT_DIR/.gemini-bridge/logs}"
LOG_FILE="${GEMINI_BRIDGE_LOG_FILE:-$LOG_DIR/gemini_bridge_server.log}"

mkdir -p "$(dirname "$PID_FILE")" "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE")"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Gemini Bridge 服务已在运行: pid=$existing_pid"
    echo "控制台: http://${GEMINI_BRIDGE_HOST:-127.0.0.1}:${GEMINI_BRIDGE_PORT:-8787}/dashboard"
    echo "健康检查: http://${GEMINI_BRIDGE_HOST:-127.0.0.1}:${GEMINI_BRIDGE_PORT:-8787}/health"
    echo "日志: $LOG_FILE"
    exit 0
  fi
fi

(
  cd "$ROOT_DIR"
  nohup "${PYTHON_BIN:-python3}" bridge/gemini_bridge_server.py >>"$LOG_FILE" 2>&1 &
  echo "$!" >"$PID_FILE"
)

echo "Gemini Bridge 服务已启动: pid=$(cat "$PID_FILE")"
echo "控制台: http://${GEMINI_BRIDGE_HOST:-127.0.0.1}:${GEMINI_BRIDGE_PORT:-8787}/dashboard"
echo "健康检查: http://${GEMINI_BRIDGE_HOST:-127.0.0.1}:${GEMINI_BRIDGE_PORT:-8787}/health"
echo "日志: $LOG_FILE"
