#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${GEMINI_BRIDGE_LAUNCH_LABEL:-com.codex.gemini-bridge}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
WORKSPACE="${GEMINI_BRIDGE_WORKSPACE:-$ROOT_DIR}"
LOG_DIR="${GEMINI_BRIDGE_LOG_DIR:-$ROOT_DIR/.gemini-bridge/logs}"
GEMINI_BIN_VALUE="${GEMINI_BIN:-$(command -v gemini || printf '%s' gemini)}"
HOST_VALUE="${GEMINI_BRIDGE_HOST:-127.0.0.1}"
PORT_VALUE="${GEMINI_BRIDGE_PORT:-8787}"
DAEMON_ENABLED_VALUE="${GEMINI_BRIDGE_DAEMON_ENABLED:-1}"
PATH_VALUE="${GEMINI_BRIDGE_PATH:-$HOME/.npm-global/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin}"
HTTP_PROXY_VALUE="${HTTP_PROXY:-${http_proxy:-}}"
HTTPS_PROXY_VALUE="${HTTPS_PROXY:-${https_proxy:-}}"
ALL_PROXY_VALUE="${ALL_PROXY:-${all_proxy:-}}"
NO_PROXY_VALUE="${NO_PROXY:-${no_proxy:-localhost,127.0.0.1}}"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

ROOT_DIR="$ROOT_DIR" \
LABEL="$LABEL" \
PLIST="$PLIST" \
PYTHON_BIN="$PYTHON_BIN" \
WORKSPACE="$WORKSPACE" \
LOG_DIR="$LOG_DIR" \
GEMINI_BIN_VALUE="$GEMINI_BIN_VALUE" \
HOST_VALUE="$HOST_VALUE" \
PORT_VALUE="$PORT_VALUE" \
DAEMON_ENABLED_VALUE="$DAEMON_ENABLED_VALUE" \
PATH_VALUE="$PATH_VALUE" \
HTTP_PROXY_VALUE="$HTTP_PROXY_VALUE" \
HTTPS_PROXY_VALUE="$HTTPS_PROXY_VALUE" \
ALL_PROXY_VALUE="$ALL_PROXY_VALUE" \
NO_PROXY_VALUE="$NO_PROXY_VALUE" \
python3 - <<'PY'
import os
import plistlib
from pathlib import Path

root = os.environ["ROOT_DIR"]
label = os.environ["LABEL"]
plist_path = Path(os.environ["PLIST"])
log_dir = Path(os.environ["LOG_DIR"])

payload = {
    "Label": label,
    "ProgramArguments": [
        os.environ["PYTHON_BIN"],
        str(Path(root) / "bridge" / "gemini_bridge_server.py"),
    ],
    "WorkingDirectory": root,
    "EnvironmentVariables": {
        "HOME": os.environ["HOME"],
        "GEMINI_CLI_HOME": os.environ["HOME"],
        "GEMINI_BRIDGE_WORKSPACE": os.environ["WORKSPACE"],
        "GEMINI_BIN": os.environ["GEMINI_BIN_VALUE"],
        "GEMINI_BRIDGE_HOST": os.environ["HOST_VALUE"],
        "GEMINI_BRIDGE_PORT": os.environ["PORT_VALUE"],
        "GEMINI_BRIDGE_DAEMON_ENABLED": os.environ["DAEMON_ENABLED_VALUE"],
        "PATH": os.environ["PATH_VALUE"],
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(log_dir / "gemini_bridge_server.launchd.out.log"),
    "StandardErrorPath": str(log_dir / "gemini_bridge_server.launchd.err.log"),
}

env = payload["EnvironmentVariables"]
for upper, lower in (
    ("HTTP_PROXY", "http_proxy"),
    ("HTTPS_PROXY", "https_proxy"),
    ("ALL_PROXY", "all_proxy"),
    ("NO_PROXY", "no_proxy"),
):
    value = os.environ.get(f"{upper}_VALUE", "")
    if value:
        env[upper] = value
        env[lower] = value

plist_path.write_bytes(plistlib.dumps(payload))
PY

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "LaunchAgent 已安装: $PLIST"
echo "Label: $LABEL"
echo "控制台: http://$HOST_VALUE:$PORT_VALUE/dashboard"
echo "健康检查: http://$HOST_VALUE:$PORT_VALUE/health"
echo "日志目录: $LOG_DIR"
