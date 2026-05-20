#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${GEMINI_BRIDGE_PID_FILE:-$ROOT_DIR/.gemini-bridge/gemini_bridge_server.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Gemini Bridge server is not running: pid file not found"
  exit 0
fi

pid="$(cat "$PID_FILE")"
if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Gemini Bridge server is not running: stale pid file removed"
  exit 0
fi

kill "$pid"
rm -f "$PID_FILE"
echo "Gemini Bridge server stopped: pid=$pid"
