#!/usr/bin/env python3
"""MCP server intended to be connected from Gemini CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SERVER_NAME = "gemini-codex-bridge"
SERVER_VERSION = "0.1.3"
REPO_ROOT = Path(__file__).resolve().parents[1]
INBOX_DIR = REPO_ROOT / ".gemini-bridge" / "inbox"
LOG_DIR = REPO_ROOT / ".gemini-bridge" / "logs"
LOG_PATH = LOG_DIR / "gemini_codex_mcp.log"
IO_MODE: str | None = None


TOOLS = [
    {
        "name": "ping",
        "description": "Verify that Gemini can call the local Gemini-Codex MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Optional message to echo back.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "workspace_snapshot",
        "description": "Return a concise snapshot of the current repository for Gemini.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_files": {
                    "type": "integer",
                    "description": "Maximum number of files to include. Defaults to 80.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "handoff_to_codex",
        "description": "Write a Gemini-to-Codex handoff message into the local inbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short handoff title.",
                },
                "body": {
                    "type": "string",
                    "description": "Detailed handoff content for Codex.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "Handoff priority. Defaults to normal.",
                },
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
    },
]


def read_message() -> dict[str, Any] | None:
    global IO_MODE
    first_line = sys.stdin.buffer.readline()
    while first_line in (b"\r\n", b"\n"):
        first_line = sys.stdin.buffer.readline()
    if not first_line:
        return None

    if not first_line.lower().startswith(b"content-length:"):
        IO_MODE = "jsonl"
        message = json.loads(first_line.decode("utf-8"))
        log_event("recv", {"method": message.get("method"), "id": message.get("id"), "io": IO_MODE})
        return message

    IO_MODE = "content-length"
    headers: dict[str, str] = {}
    line = first_line
    while True:
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("utf-8").strip()
        if ":" not in decoded:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            continue
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()
        line = sys.stdin.buffer.readline()
        if not line:
            return None

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    message = json.loads(body.decode("utf-8"))
    log_event("recv", {"method": message.get("method"), "id": message.get("id"), "io": IO_MODE})
    return message


def write_message(payload: dict[str, Any]) -> None:
    log_event("send", {"id": payload.get("id"), "has_error": "error" in payload, "io": IO_MODE})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if IO_MODE == "jsonl":
        sys.stdout.buffer.write(body + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
        sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def log_event(event: str, payload: dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **payload,
        }
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ]
    }
    if is_error:
        payload["isError"] = True
    return payload


def run_git(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<git error: {exc}>"
    output = completed.stdout.strip() or completed.stderr.strip()
    return output if output else "<empty>"


def list_repo_files(max_files: int) -> list[str]:
    ignored_dirs = {".git", "__pycache__", ".gemini-bridge"}
    files: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if len(files) >= max_files:
            break
        if path.is_dir():
            continue
        if any(part in ignored_dirs for part in path.relative_to(REPO_ROOT).parts):
            continue
        files.append(str(path.relative_to(REPO_ROOT)))
    return files


def tool_ping(arguments: dict[str, Any]) -> dict[str, Any]:
    message = str(arguments.get("message") or "pong")
    return text_result(
        "\n".join(
            [
                "Gemini-Codex MCP is reachable.",
                f"message: {message}",
                f"server: {SERVER_NAME} {SERVER_VERSION}",
                f"repo: {REPO_ROOT}",
            ]
        )
    )


def tool_workspace_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    max_files = int(arguments.get("max_files") or 80)
    files = list_repo_files(max_files)
    lines = [
        f"repo: {REPO_ROOT}",
        f"branch: {run_git(['branch', '--show-current'])}",
        "",
        "git status:",
        run_git(["status", "--short"]),
        "",
        f"files (max {max_files}):",
    ]
    lines.extend(f"- {item}" for item in files)
    return text_result("\n".join(lines))


def tool_handoff_to_codex(arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title", "")).strip()
    body = str(arguments.get("body", "")).strip()
    priority = str(arguments.get("priority") or "normal")
    if not title:
        raise ValueError("title is required.")
    if not body:
        raise ValueError("body is required.")
    if priority not in {"low", "normal", "high"}:
        raise ValueError("priority must be low, normal, or high.")

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = "".join(char if char.isalnum() else "-" for char in title.lower()).strip("-")
    safe_title = safe_title[:48] or "handoff"
    path = INBOX_DIR / f"{stamp}-{safe_title}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "gemini",
        "target": "codex",
        "priority": priority,
        "title": title,
        "body": body,
        "repo": str(REPO_ROOT),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return text_result(f"Handoff written for Codex:\n{path}")


def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "ping":
        return tool_ping(arguments)
    if name == "workspace_snapshot":
        return tool_workspace_snapshot(arguments)
    if name == "handoff_to_codex":
        return tool_handoff_to_codex(arguments)
    raise ValueError(f"Unknown tool: {name}")


def jsonrpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {},
        }

    if method == "initialize":
        params = message.get("params") or {}
        protocol_version = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {
                        "listChanged": False,
                    },
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": TOOLS,
            },
        }

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return jsonrpc_error(msg_id, -32602, "Tool arguments must be an object.")
        try:
            result = handle_tool_call(str(name), arguments)
        except ValueError as exc:
            return jsonrpc_error(msg_id, -32602, str(exc))
        except Exception as exc:  # pragma: no cover
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32603,
                    "message": str(exc),
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    if msg_id is None:
        return None

    return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


def main() -> None:
    log_event(
        "start",
        {
            "pid": os.getpid(),
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "python": sys.executable,
        },
    )
    while True:
        message = read_message()
        if message is None:
            log_event("eof", {"pid": os.getpid()})
            break
        response = handle_request(message)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
