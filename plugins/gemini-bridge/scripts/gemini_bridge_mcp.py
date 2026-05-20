#!/usr/bin/env python3
"""Minimal stdio MCP server that runs the local Gemini CLI directly."""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import shutil
import shlex
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVER_NAME = "gemini-bridge"
SERVER_VERSION = "0.7.0"
REGISTRY_VERSION = 1
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_GEMINI_HOME_CANDIDATE_RELATIVE_PATHS = (
    Path(".gemini"),
)
DEFAULT_GEMINI_AUTH_RELATIVE_PATH = Path(".gemini") / "oauth_creds.json"
DEFAULT_GEMINI_BIN_RELATIVE_PATHS = (
    Path(".npm-global") / "bin" / "gemini",
    Path(".local") / "bin" / "gemini",
)
DEFAULT_GEMINI_BIN_ABSOLUTE_PATHS = (
    Path("/opt/homebrew/bin/gemini"),
    Path("/usr/local/bin/gemini"),
    Path("/usr/bin/gemini"),
)
DEFAULT_CHILD_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
DEFAULT_WORKFLOW_RELATIVE_ROOT = Path("docs") / "ai-workflow"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_RELATIVE_PATH = Path("gemini-bridge") / "registry.json"
AUTH_PROMPT_MARKERS = (
    "Opening authentication page in your browser",
    "Do you want to continue? [Y/n]",
)
IO_MODE: str | None = None


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def configured_base_args() -> list[str]:
    return shlex.split(os.getenv("GEMINI_BASE_ARGS", ""))


def default_gemini_model() -> str:
    configured = (os.getenv("GEMINI_DEFAULT_MODEL") or "").strip()
    return configured or DEFAULT_GEMINI_MODEL


def has_model_arg(args: list[str]) -> bool:
    return any(
        arg in {"--model", "-m"} or arg.startswith("--model=") or arg.startswith("-m=")
        for arg in args
    )


def with_default_model(args: list[str]) -> list[str]:
    if has_model_arg(args):
        return args
    return ["--model", default_gemini_model(), *args]


def has_approval_mode_arg(args: list[str]) -> bool:
    return any(
        arg == "--approval-mode" or arg.startswith("--approval-mode=") or arg in {"-y", "--yolo"}
        for arg in args
    )


def with_default_approval_mode(args: list[str]) -> list[str]:
    if has_approval_mode_arg(args):
        return args
    return ["--approval-mode", "yolo", *args]


def normalize_gemini_home_root(path: str | Path) -> Path:
    resolved = resolve_path(path)
    if resolved.name == ".gemini":
        return resolved.parent
    return resolved


def candidate_home_dirs() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    raw_values = [
        os.getenv("GEMINI_CLI_HOME"),
        os.getenv("GEMINI_HOME"),
        os.getenv("HOME"),
    ]
    for raw in raw_values:
        if not raw:
            continue
        path = normalize_gemini_home_root(raw)
        if path not in seen:
            seen.add(path)
            candidates.append(path)

    home = Path.home().resolve()
    if home not in seen:
        seen.add(home)
        candidates.append(home)

    return candidates


def resolve_gemini_bin() -> str:
    configured = os.getenv("GEMINI_BIN")
    if configured:
        return configured

    discovered = shutil.which("gemini")
    if discovered:
        return discovered

    for home in candidate_home_dirs():
        for rel_path in DEFAULT_GEMINI_BIN_RELATIVE_PATHS:
            candidate = home / rel_path
            if candidate.exists():
                return str(candidate)

    for candidate in DEFAULT_GEMINI_BIN_ABSOLUTE_PATHS:
        if candidate.exists():
            return str(candidate)

    return "gemini"


def resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def default_gemini_auth_file() -> str | None:
    configured = os.getenv("GEMINI_AUTH_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured:
        return str(resolve_path(configured))

    for home in candidate_home_dirs():
        oauth_file = home / DEFAULT_GEMINI_AUTH_RELATIVE_PATH
        if oauth_file.exists():
            return str(oauth_file)

    return None


def default_gemini_home() -> str | None:
    configured = os.getenv("GEMINI_CLI_HOME") or os.getenv("GEMINI_HOME")
    if configured:
        return str(normalize_gemini_home_root(configured))

    for home in candidate_home_dirs():
        if any((home / rel_path).exists() for rel_path in DEFAULT_GEMINI_HOME_CANDIDATE_RELATIVE_PATHS):
            return str(home)

    return None


def default_child_path() -> str:
    paths: list[str] = []
    seen: set[str] = set()

    for home in candidate_home_dirs():
        for rel_path in (Path(".npm-global") / "bin", Path(".local") / "bin"):
            value = str(home / rel_path)
            if value not in seen:
                seen.add(value)
                paths.append(value)

    for value in (*DEFAULT_CHILD_PATHS, os.getenv("PATH", "")):
        if not value:
            continue
        for part in value.split(os.pathsep):
            if part and part not in seen:
                seen.add(part)
                paths.append(part)

    return os.pathsep.join(paths)


def build_child_env() -> dict[str, str]:
    child_env = os.environ.copy()

    # Ensure the child process has a good PATH for tools like ripgrep
    child_env["PATH"] = default_child_path()

    gemini_home = default_gemini_home()
    if gemini_home:
        child_env["HOME"] = gemini_home
        child_env["GEMINI_CLI_HOME"] = gemini_home

    # Do not automatically map ~/.gemini/oauth_creds.json to
    # GOOGLE_APPLICATION_CREDENTIALS. Gemini CLI user OAuth normally resolves
    # through HOME/.gemini or its keychain-backed storage; forcing ADC here can
    # make the bridge behave differently from a real terminal session.
    gemini_auth_file = os.getenv("GEMINI_AUTH_FILE")
    if gemini_auth_file:
        child_env["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolve_path(gemini_auth_file))
        child_env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"] = "false"

    # Enable autonomous mode for the bridge
    child_env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    child_env["GEMINI_CLI_AUTO_EDIT"] = "true"
    child_env["GEMINI_CLI_APPROVAL_MODE"] = "yolo"
    child_env.setdefault("NO_BROWSER", "true")

    return child_env


def registry_file_path() -> Path:
    configured = os.getenv("GEMINI_BRIDGE_REGISTRY_FILE")
    if configured:
        return resolve_path(configured)
    codex_home = os.getenv("CODEX_HOME")
    root = resolve_path(codex_home) if codex_home else Path.home().resolve() / ".codex"
    return root / DEFAULT_REGISTRY_RELATIVE_PATH


def empty_registry() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "projects": {},
        "agents": {},
        "jobs": {},
    }


def read_registry() -> dict[str, Any]:
    path = registry_file_path()
    if not path.exists():
        return empty_registry()
    registry = json.loads(path.read_text(encoding="utf-8"))
    registry.setdefault("version", REGISTRY_VERSION)
    registry.setdefault("created_at", utc_now())
    registry.setdefault("updated_at", utc_now())
    registry.setdefault("projects", {})
    registry.setdefault("agents", {})
    registry.setdefault("jobs", {})
    return registry


def write_registry(registry: dict[str, Any]) -> None:
    registry["updated_at"] = utc_now()
    write_json_atomic(registry_file_path(), registry)


def normalize_id(arguments: dict[str, Any], *names: str, required: bool = True) -> str:
    for name in names:
        value = str(arguments.get(name) or "").strip()
        if value:
            return value
    if required:
        raise ValueError(f"{' or '.join(names)} is required")
    return ""


def workflow_dir_from_project(project: dict[str, Any]) -> str:
    return str(project.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())


def resolve_project(arguments: dict[str, Any]) -> dict[str, Any] | None:
    project_id = normalize_id(arguments, "projectId", "project_id", required=False)
    if not project_id:
        return None
    registry = read_registry()
    project = dict((registry.get("projects") or {}).get(project_id) or {})
    if not project:
        raise ValueError(f"project not registered: {project_id}")
    project["projectId"] = project_id
    return project


def configure_project(project: dict[str, Any]) -> Path:
    workflow_dir = workflow_dir_from_project(project)
    return configure_workspace(project.get("workspace"), workflow_dir)


TOOLS = [
    {
        "name": "ask_gemini",
        "description": "Run the local Gemini CLI directly and return its response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Prompt to send to Gemini.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory for the Gemini CLI process.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional no-output timeout in seconds for the Gemini CLI process. Defaults to 360 and resets whenever Gemini produces output.",
                },
                "gemini_args": {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                    "description": "Optional extra Gemini CLI arguments before the prompt flag.",
                },
                "allow_fallback_strategies": {
                    "type": "boolean",
                    "description": "Allow --prompt and stdin fallback attempts. Defaults to false so the bridge always uses fixed gemini -p.",
                },
                "gemini_auth_file": {
                    "type": "string",
                    "description": "Optional advanced auth file path passed as GOOGLE_APPLICATION_CREDENTIALS. Normal Gemini CLI user OAuth should usually rely on HOME/GEMINI_HOME instead.",
                },
                "gemini_home": {
                    "type": "string",
                    "description": "Optional HOME value for Gemini. Defaults to a home directory that contains .gemini.",
                },
                "use_pty": {
                    "type": "boolean",
                    "description": "Run Gemini in a PTY so auth prompts can be detected. Defaults to true.",
                },
                "auth_confirm": {
                    "type": "string",
                    "enum": [
                        "no",
                        "yes",
                        "ignore",
                    ],
                    "description": "How to respond when Gemini asks to open browser auth. Defaults to no.",
                },
                "idle_timeout": {
                    "type": "integer",
                    "description": "Deprecated alias for timeout. Uses the same no-output timeout behavior and resets whenever Gemini produces output.",
                },
                "env_vars": {
                    "type": "object",
                    "description": "Optional environment variables to inject into the Gemini CLI process.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": [
                "prompt",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_workflow_task",
        "description": "Consume a task from docs/ai-workflow/tasks/todo/, execute it with Gemini, and capture outputs/patches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Filename of the task (without extension) in docs/ai-workflow/tasks/todo/.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional no-output timeout in seconds. Defaults to 360 and resets whenever Gemini produces output.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "register_project",
        "description": "Register a project workspace with the NexusFlow platform registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectId": {
                    "type": "string",
                    "description": "Stable project identifier used by platform tools.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Snake_case alias for projectId.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Absolute project workspace path.",
                },
                "workflow_dir": {
                    "type": "string",
                    "description": "Workflow directory relative to workspace. Defaults to docs/ai-workflow.",
                },
                "displayName": {
                    "type": "string",
                    "description": "Optional human-readable project name.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional project metadata.",
                },
            },
            "required": ["workspace"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_projects",
        "description": "List registered NexusFlow platform projects.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project",
        "description": "Get one registered NexusFlow platform project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "Project identifier."},
                "project_id": {"type": "string", "description": "Snake_case alias for projectId."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "unregister_project",
        "description": "Remove a project from the NexusFlow platform registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "Project identifier."},
                "project_id": {"type": "string", "description": "Snake_case alias for projectId."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "register_agent",
        "description": "Register a Codex, Gemini, daemon, or other worker agent with the platform registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agentId": {"type": "string", "description": "Stable agent identifier."},
                "agent_id": {"type": "string", "description": "Snake_case alias for agentId."},
                "role": {
                    "type": "string",
                    "description": "Agent role, for example codex, gemini, daemon, reviewer, or other.",
                },
                "status": {
                    "type": "string",
                    "description": "Agent status. Defaults to online.",
                },
                "projectIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional project IDs this agent can handle.",
                },
                "project_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Snake_case alias for projectIds.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional agent metadata.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_agents",
        "description": "List registered NexusFlow platform agents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Optional role filter."},
                "projectId": {"type": "string", "description": "Optional project filter."},
                "project_id": {"type": "string", "description": "Snake_case alias for projectId."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "notify_task_ready",
        "description": "Platform-safe handoff: record that Codex wrote task files for a registered project. This queues jobs but does not start Gemini.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "Registered project identifier."},
                "project_id": {"type": "string", "description": "Snake_case alias for projectId."},
                "taskId": {"type": "string", "description": "Single task ID."},
                "task_id": {"type": "string", "description": "Snake_case alias for taskId."},
                "taskIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple task IDs.",
                },
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Snake_case alias for taskIds.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional repository-relative focus files.",
                },
                "message": {"type": "string", "description": "Optional handoff note."},
                "env_vars": {
                    "type": "object",
                    "description": "Optional environment variables to inject into the Gemini CLI process.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_platform_jobs",
        "description": "List queued or dispatched platform jobs from the registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "Optional project filter."},
                "project_id": {"type": "string", "description": "Snake_case alias for projectId."},
                "status": {"type": "string", "description": "Optional job status filter."},
                "limit": {"type": "integer", "description": "Maximum jobs to return. Defaults to 20."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "start_workflow_job",
        "description": "Start an asynchronous Gemini workflow job from a task file and return immediately with a job ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Filename of the task (without extension) in docs/ai-workflow/tasks/todo/ or docs/ai-workflow/tasks/working/.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional no-output timeout in seconds. Defaults to 360 and resets whenever Gemini produces output.",
                },
                "gemini_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional extra Gemini CLI arguments before the prompt flag.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional repository-relative file paths that Gemini should focus on for this job.",
                },
                "allow_fallback_strategies": {
                    "type": "boolean",
                    "description": "Allow --prompt and stdin fallback attempts. Defaults to false.",
                },
                "env_vars": {
                    "type": "object",
                    "description": "Optional environment variables to inject into the Gemini CLI process.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "notify_workflow_ready",
        "description": "Record that Codex finished writing workflow task files, then start background Gemini jobs that read those task documents from disk by path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional single task filename without extension in docs/ai-workflow/tasks/todo/ or docs/ai-workflow/tasks/working/.",
                },
                "taskId": {
                    "type": "string",
                    "description": "CamelCase alias for task_id.",
                },
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of task filenames without extension in docs/ai-workflow/tasks/todo/ or docs/ai-workflow/tasks/working/.",
                },
                "taskIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CamelCase alias for task_ids.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional repository-relative focus paths to include as metadata only.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional no-output timeout in seconds for the background Gemini job. Defaults to 360 and resets whenever Gemini produces output.",
                },
                "gemini_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional extra Gemini CLI arguments before the prompt flag.",
                },
                "allow_fallback_strategies": {
                    "type": "boolean",
                    "description": "Allow --prompt and stdin fallback attempts. Defaults to false.",
                },
                "env_vars": {
                    "type": "object",
                    "description": "Optional environment variables to inject into the Gemini CLI process.",
                    "additionalProperties": {"type": "string"},
                },
                "message": {
                    "type": "string",
                    "description": "Optional human-readable note about the completed planning step.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_workflow_job",
        "description": "Read the status and file locations for an asynchronous workflow job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Workflow job ID returned by notify_task_ready.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
                "include_tail": {
                    "type": "boolean",
                    "description": "Include recent stdout, stderr, and events in the response.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_workflow_jobs",
        "description": "List asynchronous workflow jobs and their current statuses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of jobs to list. Defaults to 20.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_workflow_job",
        "description": "Request cancellation for an asynchronous workflow job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Workflow job ID returned by notify_task_ready.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_workflow_patch",
        "description": "Apply a patch generated during a workflow task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID whose patch should be applied.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_workflow_tests",
        "description": "Run project tests to verify changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "test_command": {
                    "type": "string",
                    "description": "Command to run tests. Defaults to 'npm test' or 'pytest' detection.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finalize_workflow_task",
        "description": "Move task to done, write a report, and clean up.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to finalize.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was done.",
                },
                "status": {
                    "type": "string",
                    "enum": ["success", "failed", "partial"],
                    "description": "Final status of the task.",
                },
            },
            "required": ["task_id", "summary", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_workflow_tasks",
        "description": "List tasks in various states (todo, working, done).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["todo", "working", "done"],
                    "description": "Filter by state. Defaults to showing all.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional target repository root. Defaults to GEMINI_BRIDGE_WORKSPACE, CODEX_WORKSPACE, a project-like cwd, or the plugin repository root.",
                },
            },
            "additionalProperties": False,
        },
    },
]

ASYNC_WORKER_TOOL_NAMES = {
    "register_project",
    "list_projects",
    "get_project",
    "unregister_project",
    "register_agent",
    "list_agents",
    "notify_task_ready",
    "list_platform_jobs",
    "get_workflow_job",
    "cancel_workflow_job",
}

TOOLS = [tool for tool in TOOLS if tool["name"] in ASYNC_WORKER_TOOL_NAMES]


def read_message() -> dict[str, Any] | None:
    global IO_MODE
    first_line = sys.stdin.buffer.readline()
    while first_line in (b"\r\n", b"\n"):
        first_line = sys.stdin.buffer.readline()
    if not first_line:
        return None

    if not first_line.lower().startswith(b"content-length:"):
        IO_MODE = "jsonl"
        return json.loads(first_line.decode("utf-8"))

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
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if IO_MODE == "jsonl":
        sys.stdout.buffer.write(body + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
        sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def make_text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ]
    }
    if is_error:
        result["isError"] = True
    return result


def normalize_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def normalize_cwd(cwd_raw: Any) -> Path | None:
    if not cwd_raw:
        return None
    cwd = Path(str(cwd_raw)).expanduser().resolve()
    if not cwd.exists():
        raise ValueError(f"cwd does not exist: {cwd}")
    if not cwd.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    return cwd


def normalize_extra_args(raw_args: Any) -> list[str]:
    if raw_args is None:
        return []
    if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
        raise ValueError("gemini_args must be an array of strings.")
    return raw_args


def normalize_string_list(raw_values: Any, name: str) -> list[str]:
    if raw_values is None:
        return []
    if not isinstance(raw_values, list) or not all(isinstance(item, str) for item in raw_values):
        raise ValueError(f"{name} must be an array of strings.")
    return [item for item in raw_values if item.strip()]


def resolve_output_timeout(arguments: dict[str, Any]) -> int:
    for key in ("timeout", "idle_timeout"):
        value = arguments.get(key)
        if value not in (None, ""):
            timeout = int(value)
            if timeout <= 0:
                raise ValueError(f"{key} must be a positive integer.")
            return timeout

    for env_name in ("GEMINI_TIMEOUT_SECONDS", "GEMINI_IDLE_TIMEOUT_SECONDS"):
        raw = os.getenv(env_name)
        if raw not in (None, ""):
            timeout = int(raw)
            if timeout <= 0:
                raise ValueError(f"{env_name} must be a positive integer.")
            return timeout

    return 360


def read_available_fd(fd: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    eof = False
    while True:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            break
        except OSError:
            eof = True
            break
        if not data:
            eof = True
            break
        chunks.append(data)
        if len(data) < 4096:
            break
    return b"".join(chunks), eof


def run_gemini(arguments: dict[str, Any]) -> dict[str, Any]:
    prompt = str(arguments.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Tool argument 'prompt' is required.")

    output_timeout = resolve_output_timeout(arguments)
    cwd = normalize_cwd(arguments.get("cwd"))
    gemini_bin = resolve_gemini_bin()
    raw_args = [*configured_base_args(), *normalize_extra_args(arguments.get("gemini_args"))]
    common_args = with_default_approval_mode(with_default_model(raw_args))
    child_env = build_child_env()
    env_vars = arguments.get("env_vars")
    if isinstance(env_vars, dict):
        for k, v in env_vars.items():
            if v is not None:
                child_env[k] = str(v)

    allow_fallback_strategies = bool(arguments.get("allow_fallback_strategies", False))
    use_pty = bool(arguments.get("use_pty", True))
    auth_confirm = str(arguments.get("auth_confirm") or "no")
    if auth_confirm not in {"no", "yes", "ignore"}:
        raise ValueError("auth_confirm must be one of: no, yes, ignore.")
    if arguments.get("gemini_home"):
        gemini_home = str(normalize_gemini_home_root(str(arguments["gemini_home"])))
        child_env["HOME"] = gemini_home
        child_env["GEMINI_CLI_HOME"] = gemini_home
    if arguments.get("gemini_auth_file"):
        child_env["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolve_path(str(arguments["gemini_auth_file"])))
        child_env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"] = "false"

    attempts = [
        {
            "strategy": "flag_-p",
            "command": [gemini_bin, *common_args, "-p", prompt],
            "stdin": None,
        }
    ]
    if allow_fallback_strategies:
        attempts.extend(
            [
                {
                    "strategy": "flag_--prompt",
                    "command": [gemini_bin, *common_args, "--prompt", prompt],
                    "stdin": None,
                },
                {
                    "strategy": "stdin",
                    "command": [gemini_bin, *common_args],
                    "stdin": prompt,
                },
            ]
        )

    failures: list[dict[str, Any]] = []
    for attempt in attempts:
        try:
            completed = run_attempt(
                command=list(attempt["command"]),
                stdin_text=attempt["stdin"],
                cwd=cwd,
                child_env=child_env,
                output_timeout=output_timeout,
                use_pty=use_pty,
                auth_confirm=auth_confirm,
            )
        except FileNotFoundError:
            return make_text_result(
                f"Gemini executable not found: {gemini_bin}",
                is_error=True,
            )

        if completed["timed_out"] or completed["auth_prompt_detected"] or completed["idle_timed_out"]:
            status = completed["status"]
            return format_process_report(
                status=status,
                strategy=str(attempt["strategy"]),
                command=list(attempt["command"]),
                cwd=cwd,
                child_env=child_env,
                returncode=completed["returncode"],
                stdout=completed["stdout"],
                stderr=completed["stderr"],
                is_error=True,
            )

        if completed["returncode"] == 0:
            return format_process_report(
                status="success",
                strategy=str(attempt["strategy"]),
                command=list(attempt["command"]),
                cwd=cwd,
                child_env=child_env,
                returncode=completed["returncode"],
                stdout=completed["stdout"],
                stderr=completed["stderr"],
            )

        failures.append(
            {
                "strategy": attempt["strategy"],
                "command": attempt["command"],
                "returncode": completed["returncode"],
                "stdout": completed["stdout"].strip(),
                "stderr": completed["stderr"].strip(),
            }
        )

    return format_failures(failures, cwd=cwd, child_env=child_env)


def run_attempt(
    *,
    command: list[str],
    stdin_text: str | None,
    cwd: Path | None,
    child_env: dict[str, str],
    output_timeout: int,
    use_pty: bool,
    auth_confirm: str,
) -> dict[str, Any]:
    if use_pty:
        return run_attempt_pty(
            command=command,
            stdin_text=stdin_text,
            cwd=cwd,
            child_env=child_env,
            output_timeout=output_timeout,
            auth_confirm=auth_confirm,
        )
    return run_attempt_plain(
        command=command,
        stdin_text=stdin_text,
        cwd=cwd,
        child_env=child_env,
        output_timeout=output_timeout,
    )


def run_attempt_plain(
    *,
    command: list[str],
    stdin_text: str | None,
    cwd: Path | None,
    child_env: dict[str, str],
    output_timeout: int,
) -> dict[str, Any]:
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=child_env,
        close_fds=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    streams: dict[int, str] = {}
    status = "completed"
    idle_timed_out = False

    assert proc.stdout is not None
    assert proc.stderr is not None
    for pipe, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        streams[fd] = name

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()

    last_output_at = time.time()
    try:
        while True:
            if not streams and proc.poll() is not None:
                break

            remaining = output_timeout - (time.time() - last_output_at)
            if remaining <= 0:
                idle_timed_out = True
                status = f"no output timeout after {output_timeout} seconds"
                proc.terminate()
                break

            ready, _, _ = select.select(list(streams), [], [], min(0.25, remaining))
            target_chunks: list[str] | None = None
            if ready:
                for fd in ready:
                    data, eof = read_available_fd(fd)
                    stream_name = streams.get(fd)
                    if data and stream_name == "stdout":
                        target_chunks = stdout_chunks
                    elif data and stream_name == "stderr":
                        target_chunks = stderr_chunks
                    else:
                        target_chunks = None
                    if target_chunks is not None:
                        target_chunks.append(data.decode("utf-8", errors="replace"))
                        last_output_at = time.time()
                    if eof:
                        streams.pop(fd, None)
            elif proc.poll() is not None:
                for fd in list(streams):
                    data, eof = read_available_fd(fd)
                    stream_name = streams.get(fd)
                    if data and stream_name == "stdout":
                        stdout_chunks.append(data.decode("utf-8", errors="replace"))
                        last_output_at = time.time()
                    elif data and stream_name == "stderr":
                        stderr_chunks.append(data.decode("utf-8", errors="replace"))
                        last_output_at = time.time()
                    if eof:
                        streams.pop(fd, None)
                if not streams:
                    break
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    if idle_timed_out:
        return {
            "status": status,
            "returncode": proc.returncode,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "timed_out": False,
            "idle_timed_out": True,
            "auth_prompt_detected": False,
        }
    return {
        "status": status,
        "returncode": proc.returncode,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
        "timed_out": False,
        "idle_timed_out": False,
        "auth_prompt_detected": False,
    }


def run_attempt_pty(
    *,
    command: list[str],
    stdin_text: str | None,
    cwd: Path | None,
    child_env: dict[str, str],
    output_timeout: int,
    auth_confirm: str,
) -> dict[str, Any]:
    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    chunks: list[str] = []
    last_output_at = time.time()
    idle_timed_out = False
    auth_prompt_detected = False
    status = "completed"
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        if stdin_text:
            os.write(master_fd, stdin_text.encode("utf-8") + b"\n")
        while True:
            remaining = output_timeout - (time.time() - last_output_at)
            if remaining <= 0:
                idle_timed_out = True
                status = f"no output timeout after {output_timeout} seconds"
                proc.terminate()
                break
            ready, _, _ = select.select([master_fd], [], [], min(0.25, remaining))
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                chunks.append(text)
                last_output_at = time.time()
                recent = "".join(chunks[-5:])
                if not auth_prompt_detected and any(marker in recent for marker in AUTH_PROMPT_MARKERS):
                    auth_prompt_detected = True
                    if auth_confirm == "yes":
                        os.write(master_fd, b"y\n")
                        chunks.append("\n[gemini-bridge] detected Gemini auth prompt, answered yes\n")
                    elif auth_confirm == "no":
                        os.write(master_fd, b"n\n")
                        chunks.append(
                            "\n[gemini-bridge] detected Gemini auth prompt, answered no. "
                            "Run Gemini once in a real terminal or call with auth_confirm=yes.\n"
                        )
                        status = "Gemini auth prompt detected"
                        proc.terminate()
                        break
            if proc.poll() is not None:
                break
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass
    return {
        "status": status,
        "returncode": proc.returncode if proc else None,
        "stdout": "".join(chunks),
        "stderr": "<PTY mode combines stdout and stderr>",
        "timed_out": False,
        "idle_timed_out": idle_timed_out,
        "auth_prompt_detected": auth_prompt_detected and auth_confirm == "no",
    }


def format_output_block(label: str, value: str) -> str:
    text = value.strip()
    return f"[gemini {label}]\n{text if text else '<empty>'}"


def format_process_report(
    *,
    status: str,
    strategy: str,
    command: list[str],
    cwd: Path | None,
    child_env: dict[str, str],
    returncode: int | None,
    stdout: str,
    stderr: str,
    is_error: bool = False,
) -> dict[str, Any]:
    sections = [
        f"Status: {status}",
        f"Gemini strategy: {strategy}",
        f"Command: {json.dumps(command, ensure_ascii=False)}",
        f"cwd: {cwd if cwd else '<default>'}",
        f"HOME: {child_env.get('HOME', '<unset>')}",
        f"GOOGLE_APPLICATION_CREDENTIALS: {child_env.get('GOOGLE_APPLICATION_CREDENTIALS', '<unset>')}",
        f"GEMINI_FORCE_ENCRYPTED_FILE_STORAGE: {child_env.get('GEMINI_FORCE_ENCRYPTED_FILE_STORAGE', '<unset>')}",
        f"returncode: {returncode if returncode is not None else '<none>'}",
        format_output_block("stdout", stdout),
        format_output_block("stderr", stderr),
    ]
    return make_text_result("\n\n".join(sections).strip(), is_error=is_error)


def format_failures(
    failures: list[dict[str, Any]],
    *,
    cwd: Path | None,
    child_env: dict[str, str],
) -> dict[str, Any]:
    sections = [
        "Status: failed",
        "All Gemini invocation strategies failed.",
        f"cwd: {cwd if cwd else '<default>'}",
        f"HOME: {child_env.get('HOME', '<unset>')}",
        f"GOOGLE_APPLICATION_CREDENTIALS: {child_env.get('GOOGLE_APPLICATION_CREDENTIALS', '<unset>')}",
        f"GEMINI_FORCE_ENCRYPTED_FILE_STORAGE: {child_env.get('GEMINI_FORCE_ENCRYPTED_FILE_STORAGE', '<unset>')}",
    ]
    for failure in failures:
        sections.extend(
            [
                f"Gemini strategy: {failure['strategy']}",
                f"Command: {json.dumps(failure['command'], ensure_ascii=False)}",
                f"returncode: {failure['returncode']}",
                format_output_block("stdout", str(failure.get("stdout", ""))),
                format_output_block("stderr", str(failure.get("stderr", ""))),
            ]
        )
    return make_text_result(
        "\n\n".join(sections).strip(),
        is_error=True,
    )


def resolve_workspace_root(raw_workspace: Any = None) -> Path:
    raw = raw_workspace or os.getenv("GEMINI_BRIDGE_WORKSPACE") or os.getenv("CODEX_WORKSPACE")
    if raw:
        root = resolve_path(str(raw))
        if not root.exists():
            raise ValueError(f"workspace does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        return root

    cwd = Path.cwd().resolve()
    if (cwd / ".git").exists() or (cwd / "package.json").exists() or (cwd / "pyproject.toml").exists():
        return cwd

    return DEFAULT_REPO_ROOT


def resolve_workflow_relative_root(raw_workflow_dir: Any = None) -> Path:
    raw = raw_workflow_dir or os.getenv("GEMINI_BRIDGE_WORKFLOW_DIR")
    return Path(raw) if raw else DEFAULT_WORKFLOW_RELATIVE_ROOT


def configure_workspace(raw_workspace: Any = None, raw_workflow_dir: Any = None) -> Path:
    global REPO_ROOT, AI_WORKFLOW_RELATIVE_ROOT, AI_WORKFLOW_ROOT, WORKFLOW_ROOT, JOBS_ROOT, SIGNALS_ROOT

    REPO_ROOT = resolve_workspace_root(raw_workspace)
    AI_WORKFLOW_RELATIVE_ROOT = resolve_workflow_relative_root(raw_workflow_dir)
    AI_WORKFLOW_ROOT = REPO_ROOT / AI_WORKFLOW_RELATIVE_ROOT
    WORKFLOW_ROOT = AI_WORKFLOW_ROOT / "tasks"
    JOBS_ROOT = AI_WORKFLOW_ROOT / "jobs"
    SIGNALS_ROOT = AI_WORKFLOW_ROOT / "signals"
    return REPO_ROOT


REPO_ROOT = DEFAULT_REPO_ROOT
AI_WORKFLOW_RELATIVE_ROOT = DEFAULT_WORKFLOW_RELATIVE_ROOT
AI_WORKFLOW_ROOT = REPO_ROOT / AI_WORKFLOW_RELATIVE_ROOT
WORKFLOW_ROOT = AI_WORKFLOW_ROOT / "tasks"
JOBS_ROOT = AI_WORKFLOW_ROOT / "jobs"
SIGNALS_ROOT = AI_WORKFLOW_ROOT / "signals"
configure_workspace()


def get_workflow_paths(task_id: str) -> dict[str, Path]:
    return {
        "todo": WORKFLOW_ROOT / "todo" / f"{task_id}.md",
        "working": WORKFLOW_ROOT / "working" / f"{task_id}.md",
        "done": WORKFLOW_ROOT / "done" / f"{task_id}.md",
        "patch": WORKFLOW_ROOT / "patches" / f"{task_id}.patch",
        "output": WORKFLOW_ROOT / "outputs" / f"{task_id}.log",
        "report": WORKFLOW_ROOT / "reports" / f"{task_id}.md",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_job_paths(job_id: str) -> dict[str, Path]:
    job_dir = JOBS_ROOT / job_id
    return {
        "dir": job_dir,
        "status": job_dir / "status.json",
        "events": job_dir / "events.log",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
        "result": job_dir / "result.md",
        "patch": job_dir / "patch.patch",
        "task": job_dir / "task.md",
        "cancel": job_dir / "cancel.requested",
    }


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def append_job_event(job_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
    paths = get_job_paths(job_id)
    paths["events"].parent.mkdir(parents=True, exist_ok=True)
    line = {
        "time": utc_now(),
        "event": event,
        **(payload or {}),
    }
    with paths["events"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def update_job_status(job_id: str, **updates: Any) -> dict[str, Any]:
    paths = get_job_paths(job_id)
    status = read_json(paths["status"])
    status.update(updates)
    status["updated_at"] = utc_now()
    write_json_atomic(paths["status"], status)
    return status


def tail_file(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def is_workflow_artifact(path: str) -> bool:
    workflow_path = AI_WORKFLOW_RELATIVE_ROOT.as_posix().strip("/")
    return (
        path == workflow_path
        or path.startswith(f"{workflow_path}/")
        or path == ".ai-workflow"
        or path.startswith(".ai-workflow/")
    )


def list_untracked_repo_files() -> set[str]:
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not is_workflow_artifact(line.strip())
    }


def capture_workflow_patch(before_untracked: set[str]) -> str:
    patch_parts: list[str] = []

    diff_proc = subprocess.run(
        [
            "git",
            "diff",
            "--",
            ".",
            f":(exclude){AI_WORKFLOW_RELATIVE_ROOT.as_posix()}",
            f":(exclude){AI_WORKFLOW_RELATIVE_ROOT.as_posix()}/**",
            ":(exclude).ai-workflow",
            ":(exclude).ai-workflow/**",
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    if diff_proc.stdout:
        patch_parts.append(diff_proc.stdout)

    after_untracked = list_untracked_repo_files()
    new_files = sorted(after_untracked - before_untracked)
    for rel_path in new_files:
        file_path = REPO_ROOT / rel_path
        if not file_path.is_file():
            continue
        new_file_diff = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", rel_path],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        if new_file_diff.stdout:
            patch_parts.append(new_file_diff.stdout)

    return "\n".join(part.rstrip() for part in patch_parts if part.strip()) + ("\n" if patch_parts else "")


def extract_stdout_patch(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return ""

    lines = text.splitlines()

    for index, line in enumerate(lines):
        if line.strip() in {"```diff", "```patch", "```"}:
            block: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate.strip() == "```":
                    break
                block.append(candidate)
            block_text = "\n".join(block).strip()
            if "+++ " in block_text and ("--- " in block_text or "diff --git " in block_text):
                return block_text + "\n"

    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            candidate = "\n".join(lines[index:]).strip()
            if "+++ " in candidate and ("@@ " in candidate or candidate.startswith("diff --git ")):
                return candidate + "\n"

    return ""


def build_gemini_attempts(arguments: dict[str, Any]) -> tuple[list[dict[str, Any]], Path | None, dict[str, str], int]:
    prompt = str(arguments.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Tool argument 'prompt' is required.")

    output_timeout = resolve_output_timeout(arguments)
    cwd = normalize_cwd(arguments.get("cwd"))
    gemini_bin = resolve_gemini_bin()
    raw_args = [*configured_base_args(), *normalize_extra_args(arguments.get("gemini_args"))]
    common_args = with_default_approval_mode(with_default_model(raw_args))
    child_env = build_child_env()
    env_vars = arguments.get("env_vars")
    if isinstance(env_vars, dict):
        for k, v in env_vars.items():
            if v is not None:
                child_env[k] = str(v)

    if arguments.get("gemini_home"):
        gemini_home = str(normalize_gemini_home_root(str(arguments["gemini_home"])))
        child_env["HOME"] = gemini_home
        child_env["GEMINI_CLI_HOME"] = gemini_home
    if arguments.get("gemini_auth_file"):
        child_env["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolve_path(str(arguments["gemini_auth_file"])))
        child_env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"] = "false"

    attempts = [
        {
            "strategy": "flag_-p",
            "command": [gemini_bin, *common_args, "-p", prompt],
            "stdin": None,
        }
    ]
    if common_args != raw_args and env_bool("GEMINI_FALLBACK_TO_CLI_DEFAULT_MODEL", True):
        attempts.append(
            {
                "strategy": "flag_-p_cli_default_model",
                "command": [gemini_bin, *raw_args, "-p", prompt],
                "stdin": None,
            }
        )
    if bool(arguments.get("allow_fallback_strategies", False)):
        attempts.extend(
            [
                {
                    "strategy": "flag_--prompt",
                    "command": [gemini_bin, *common_args, "--prompt", prompt],
                    "stdin": None,
                },
                {
                    "strategy": "stdin",
                    "command": [gemini_bin, *common_args],
                    "stdin": prompt,
                },
            ]
        )

    return attempts, cwd, child_env, output_timeout


def run_attempt_streaming_to_job(
    *,
    job_id: str,
    command: list[str],
    stdin_text: str | None,
    cwd: Path | None,
    child_env: dict[str, str],
    output_timeout: int,
) -> dict[str, Any]:
    paths = get_job_paths(job_id)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=child_env,
        close_fds=True,
        start_new_session=True,
    )
    update_job_status(job_id, worker_pid=os.getpid(), gemini_pid=proc.pid)

    assert proc.stdout is not None
    assert proc.stderr is not None
    streams: dict[int, str] = {}
    for pipe, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        streams[fd] = name

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()

    paths["stdout"].parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = paths["stdout"].open("a", encoding="utf-8")
    stderr_handle = paths["stderr"].open("a", encoding="utf-8")
    last_output_at = time.time()
    stdout_size = 0
    stderr_size = 0
    status = "completed"
    idle_timed_out = False
    cancelled = False
    auth_prompt_detected = False
    recent_output = ""
    try:
        while True:
            if paths["cancel"].exists():
                cancelled = True
                status = "cancelled"
                proc.terminate()
                break

            if not streams and proc.poll() is not None:
                break

            remaining = output_timeout - (time.time() - last_output_at)
            if remaining <= 0:
                idle_timed_out = True
                status = f"no output timeout after {output_timeout} seconds"
                proc.terminate()
                break

            ready, _, _ = select.select(list(streams), [], [], min(0.25, remaining))
            if ready:
                for fd in ready:
                    data, eof = read_available_fd(fd)
                    stream_name = streams.get(fd)
                    if data and stream_name:
                        text = data.decode("utf-8", errors="replace")
                        if stream_name == "stdout":
                            stdout_handle.write(text)
                            stdout_handle.flush()
                            stdout_size += len(text)
                        else:
                            stderr_handle.write(text)
                            stderr_handle.flush()
                            stderr_size += len(text)
                        last_output_at = time.time()
                        recent_output = (recent_output + text)[-4000:]
                        if not auth_prompt_detected and any(marker in recent_output for marker in AUTH_PROMPT_MARKERS):
                            auth_prompt_detected = True
                            status = "Gemini auth prompt detected"
                            stderr_handle.write(
                                "\n[gemini-bridge] detected Gemini auth prompt. "
                                "Run `gemini` once in a real terminal to complete login, "
                                "or configure GEMINI_HOME/GEMINI_AUTH_FILE for the MCP process.\n"
                            )
                            stderr_handle.flush()
                            proc.terminate()
                            break
                        update_job_status(
                            job_id,
                            status="running",
                            stdout_bytes=stdout_size,
                            stderr_bytes=stderr_size,
                            last_output_at=utc_now(),
                        )
                    if eof:
                        streams.pop(fd, None)
            elif proc.poll() is not None:
                for fd in list(streams):
                    data, eof = read_available_fd(fd)
                    stream_name = streams.get(fd)
                    if data and stream_name:
                        text = data.decode("utf-8", errors="replace")
                        if stream_name == "stdout":
                            stdout_handle.write(text)
                            stdout_handle.flush()
                            stdout_size += len(text)
                        else:
                            stderr_handle.write(text)
                            stderr_handle.flush()
                            stderr_size += len(text)
                        last_output_at = time.time()
                        recent_output = (recent_output + text)[-4000:]
                    if eof:
                        streams.pop(fd, None)
                if not streams:
                    break

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        stdout_handle.close()
        stderr_handle.close()
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    return {
        "status": status,
        "returncode": proc.returncode,
        "timed_out": False,
        "idle_timed_out": idle_timed_out,
        "cancelled": cancelled,
        "auth_prompt_detected": auth_prompt_detected,
        "stdout": tail_file(paths["stdout"], 12000),
        "stderr": tail_file(paths["stderr"], 12000),
    }


def run_gemini_streaming_job(job_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    attempts, cwd, child_env, output_timeout = build_gemini_attempts(arguments)
    failures: list[dict[str, Any]] = []
    for attempt in attempts:
        append_job_event(
            job_id,
            "gemini_attempt_started",
            {
                "strategy": attempt["strategy"],
                "command": attempt["command"],
                "cwd": str(cwd if cwd else Path.cwd()),
            },
        )
        try:
            completed = run_attempt_streaming_to_job(
                job_id=job_id,
                command=list(attempt["command"]),
                stdin_text=attempt["stdin"],
                cwd=cwd,
                child_env=child_env,
                output_timeout=output_timeout,
            )
        except FileNotFoundError:
            return {
                "status": "Gemini executable not found",
                "returncode": None,
                "stdout": "",
                "stderr": f"Gemini executable not found: {attempt['command'][0]}",
                "is_error": True,
            }

        append_job_event(
            job_id,
            "gemini_attempt_finished",
            {
                "strategy": attempt["strategy"],
                "returncode": completed["returncode"],
                "status": completed["status"],
            },
        )

        if completed["auth_prompt_detected"]:
            completed["is_error"] = True
            return completed

        if completed["cancelled"] or completed["idle_timed_out"]:
            completed["is_error"] = True
            return completed

        if completed["returncode"] == 0:
            completed["is_error"] = False
            return completed

        failures.append(
            {
                "strategy": attempt["strategy"],
                "command": attempt["command"],
                "returncode": completed["returncode"],
                "stdout": completed["stdout"].strip(),
                "stderr": completed["stderr"].strip(),
            }
        )

    return {
        "status": "failed",
        "returncode": failures[-1]["returncode"] if failures else None,
        "stdout": failures[-1]["stdout"] if failures else "",
        "stderr": json.dumps(failures, ensure_ascii=False, indent=2),
        "is_error": True,
    }


def build_task_path_prompt(task_id: str, task_file: str, files: list[str]) -> str:
    sections = [
        "Execute a workflow task from a local task document.",
        "",
        "Important rules:",
        "- Codex has only signaled that the task file is ready.",
        "- The task body was not sent through MCP.",
        "- Read the task document directly from the repository checkout.",
        "",
        f"Repository root: {REPO_ROOT}",
        f"Task ID: {task_id}",
        f"Task document path: {task_file}",
        "",
        "Instructions:",
        "1. Read the task document at the path above.",
        "2. Use the taskId inside that document as the source of truth.",
        "3. Run autonomously. Do not ask for human confirmation or approval.",
        "4. Implement the requested work in the repository.",
        "5. Print a concise summary of changes and verification steps.",
        "6. If direct file editing tools are unavailable, print a complete unified diff patch instead of waiting for confirmation.",
    ]
    if files:
        sections.extend(
            [
                "",
                "Focus files from the signal:",
                *[f"- {item}" for item in files],
            ]
        )
    return "\n".join(sections)


def workflow_job_worker(job_id: str) -> int:
    paths = get_job_paths(job_id)
    status = read_json(paths["status"])
    task_id = str(status.get("task_id") or "")
    if not task_id:
        update_job_status(job_id, status="failed", error="Missing task_id", finished_at=utc_now())
        return 2

    append_job_event(job_id, "worker_started", {"pid": os.getpid(), "task_id": task_id})
    update_job_status(job_id, status="running", started_at=utc_now(), worker_pid=os.getpid())
    try:
        before_untracked = list_untracked_repo_files()
        gemini_args = dict(status.get("arguments") or {})
        files = normalize_string_list(status.get("files"), "files")
        task_file = str(status.get("task_file") or paths["task"].relative_to(REPO_ROOT))
        gemini_args["prompt"] = build_task_path_prompt(task_id, task_file, files)
        gemini_args["cwd"] = str(REPO_ROOT)
        update_job_status(job_id, prompt_mode="task_path", prompt_task_file=task_file)
        append_job_event(job_id, "gemini_prompt_ready", {"prompt_mode": "task_path", "task_file": task_file})

        result = run_gemini_streaming_job(job_id, gemini_args)
        paths["result"].write_text(
            "\n\n".join(
                [
                    f"Status: {result.get('status')}",
                    f"returncode: {result.get('returncode')}",
                    "[gemini stdout tail]",
                    str(result.get("stdout") or "").strip() or "<empty>",
                    "[gemini stderr tail]",
                    str(result.get("stderr") or "").strip() or "<empty>",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        task_paths = get_workflow_paths(task_id)
        task_paths["output"].parent.mkdir(parents=True, exist_ok=True)
        task_paths["patch"].parent.mkdir(parents=True, exist_ok=True)
        task_paths["output"].write_text(paths["result"].read_text(encoding="utf-8"), encoding="utf-8")

        final_status = "succeeded" if not result.get("is_error") and result.get("returncode") == 0 else "failed"
        if result.get("cancelled"):
            final_status = "cancelled"
        patch_source = "none"
        if final_status == "succeeded":
            patch_text = capture_workflow_patch(before_untracked)
            patch_source = "git_diff" if patch_text.strip() else "none"
            patch_skipped_reason = None
            if not patch_text.strip():
                stdout_patch = extract_stdout_patch(str(result.get("stdout") or ""))
                if stdout_patch:
                    patch_text = stdout_patch
                    patch_source = "gemini_stdout"
        else:
            patch_text = ""
            patch_skipped_reason = f"Gemini did not complete successfully: {result.get('status')}"
            append_job_event(job_id, "patch_capture_skipped", {"reason": patch_skipped_reason})
        paths["patch"].write_text(patch_text, encoding="utf-8")
        task_paths["patch"].write_text(patch_text, encoding="utf-8")

        update_job_status(
            job_id,
            status=final_status,
            finished_at=utc_now(),
            returncode=result.get("returncode"),
            result_file=str(paths["result"].relative_to(REPO_ROOT)),
            patch_file=str(paths["patch"].relative_to(REPO_ROOT)),
            task_output_file=str(task_paths["output"].relative_to(REPO_ROOT)),
            task_patch_file=str(task_paths["patch"].relative_to(REPO_ROOT)),
            patch_source=patch_source,
            patch_skipped_reason=patch_skipped_reason,
        )
        append_job_event(job_id, "worker_finished", {"status": final_status})
        return 0 if final_status == "succeeded" else 1
    except Exception as exc:
        paths["stderr"].parent.mkdir(parents=True, exist_ok=True)
        with paths["stderr"].open("a", encoding="utf-8") as handle:
            handle.write(f"\n[worker error]\n{traceback.format_exc()}\n")
        update_job_status(job_id, status="failed", error=str(exc), finished_at=utc_now())
        append_job_event(job_id, "worker_failed", {"error": str(exc)})
        return 1


def tool_list_workflow_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
    state_filter = arguments.get("state")
    states = ["todo", "working", "done"] if not state_filter else [state_filter]
    
    result = []
    for state in states:
        dir_path = WORKFLOW_ROOT / state
        if not dir_path.exists():
            continue
        tasks = [f.stem for f in dir_path.glob("*.md")]
        if tasks:
            result.append(f"[{state}]\n" + "\n".join(f"- {t}" for t in sorted(tasks)))
    
    if not result:
        return make_text_result("No tasks found.")
    return make_text_result("\n\n".join(result))


def tool_run_workflow_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id", "")).strip()
    if not task_id:
        raise ValueError("task_id is required")

    paths = get_workflow_paths(task_id)
    if not paths["todo"].exists():
        if paths["working"].exists():
            return make_text_result(f"Task {task_id} is already in progress.", is_error=True)
        return make_text_result(f"Task file not found: {paths['todo']}", is_error=True)

    # Move to working
    paths["working"].parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(paths["todo"]), str(paths["working"]))

    before_untracked = list_untracked_repo_files()
    
    # Run Gemini
    gemini_args = arguments.copy()
    task_file = str(paths["working"].relative_to(REPO_ROOT))
    gemini_args["prompt"] = build_task_path_prompt(task_id, task_file, [])
    # Ensure we run in REPO_ROOT
    gemini_args["cwd"] = str(REPO_ROOT)
    
    result = run_gemini(gemini_args)
    
    # Capture output
    output_text = ""
    if "content" in result and result["content"]:
        output_text = result["content"][0].get("text", "")
    
    paths["output"].parent.mkdir(parents=True, exist_ok=True)
    paths["output"].write_text(output_text, encoding="utf-8")
    
    paths["patch"].parent.mkdir(parents=True, exist_ok=True)
    if result.get("isError"):
        patch_text = ""
        paths["patch"].write_text(patch_text, encoding="utf-8")
    else:
        # Capture only changes produced by Gemini, without mutating the user's git index.
        try:
            patch_text = capture_workflow_patch(before_untracked)
            if not patch_text.strip():
                patch_text = extract_stdout_patch(output_text)
            paths["patch"].write_text(patch_text, encoding="utf-8")
        except Exception as exc:
            patch_text = f"Error capturing diff: {exc}"

    summary = [
        f"Task {task_id} executed.",
        f"Output saved to: {paths['output'].relative_to(REPO_ROOT)}",
        f"Patch saved to: {paths['patch'].relative_to(REPO_ROOT)}",
        "",
        "--- Gemini Output Snippet ---",
        output_text[:500] + ("..." if len(output_text) > 500 else ""),
        "",
        "--- Git Diff Snippet ---",
        patch_text[:500] + ("..." if len(patch_text) > 500 else ""),
    ]
    
    return make_text_result("\n".join(summary))


def normalize_task_ids(arguments: dict[str, Any]) -> list[str]:
    task_ids = [
        *normalize_string_list(arguments.get("task_ids"), "task_ids"),
        *normalize_string_list(arguments.get("taskIds"), "taskIds"),
    ]
    single_task_id = str(arguments.get("task_id") or arguments.get("taskId") or "").strip()
    if single_task_id:
        task_ids = [single_task_id, *[item for item in task_ids if item != single_task_id]]
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        raise ValueError("taskId or taskIds is required")
    return task_ids


def tool_register_project(arguments: dict[str, Any]) -> dict[str, Any]:
    workspace = resolve_workspace_root(arguments.get("workspace"))
    project_id = normalize_id(arguments, "projectId", "project_id", required=False) or workspace.name
    workflow_dir = str(arguments.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix()).strip()
    display_name = str(arguments.get("displayName") or project_id).strip()
    metadata = arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else {}

    workflow_root = workspace / workflow_dir
    for relative in (
        "tasks/todo",
        "tasks/working",
        "tasks/done",
        "tasks/outputs",
        "tasks/patches",
        "tasks/reports",
        "jobs",
        "signals",
    ):
        (workflow_root / relative).mkdir(parents=True, exist_ok=True)

    registry = read_registry()
    projects = registry.setdefault("projects", {})
    existing = dict(projects.get(project_id) or {})
    projects[project_id] = {
        "projectId": project_id,
        "displayName": display_name,
        "workspace": str(workspace),
        "workflow_dir": workflow_dir,
        "status": "active",
        "registered_at": existing.get("registered_at") or utc_now(),
        "updated_at": utc_now(),
        "metadata": metadata,
    }
    write_registry(registry)
    return make_text_result(
        "\n".join(
            [
                "Project registered.",
                f"projectId: {project_id}",
                f"workspace: {workspace}",
                f"workflow_dir: {workflow_dir}",
                f"registry: {registry_file_path()}",
            ]
        )
    )


def tool_list_projects(arguments: dict[str, Any]) -> dict[str, Any]:
    registry = read_registry()
    projects = registry.get("projects") or {}
    if not projects:
        return make_text_result("No projects registered.")
    rows = [
        {
            "projectId": project_id,
            **project,
        }
        for project_id, project in sorted(projects.items())
    ]
    return make_text_result(json.dumps(rows, ensure_ascii=False, indent=2))


def tool_get_project(arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = normalize_id(arguments, "projectId", "project_id")
    registry = read_registry()
    project = (registry.get("projects") or {}).get(project_id)
    if not project:
        return make_text_result(f"Project not found: {project_id}", is_error=True)
    return make_text_result(json.dumps({"projectId": project_id, **project}, ensure_ascii=False, indent=2))


def tool_unregister_project(arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = normalize_id(arguments, "projectId", "project_id")
    registry = read_registry()
    projects = registry.setdefault("projects", {})
    if project_id not in projects:
        return make_text_result(f"Project not found: {project_id}", is_error=True)
    projects.pop(project_id)
    write_registry(registry)
    return make_text_result(f"Project unregistered: {project_id}")


def tool_register_agent(arguments: dict[str, Any]) -> dict[str, Any]:
    role = str(arguments.get("role") or "other").strip()
    agent_id = normalize_id(arguments, "agentId", "agent_id", required=False) or f"{role}-{uuid.uuid4().hex[:8]}"
    status = str(arguments.get("status") or "online").strip()
    project_ids = [
        *normalize_string_list(arguments.get("projectIds"), "projectIds"),
        *normalize_string_list(arguments.get("project_ids"), "project_ids"),
    ]
    project_ids = list(dict.fromkeys(project_ids))
    metadata = arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else {}

    registry = read_registry()
    agents = registry.setdefault("agents", {})
    existing = dict(agents.get(agent_id) or {})
    agents[agent_id] = {
        "agentId": agent_id,
        "role": role,
        "status": status,
        "projectIds": project_ids,
        "registered_at": existing.get("registered_at") or utc_now(),
        "last_seen_at": utc_now(),
        "metadata": metadata,
    }
    write_registry(registry)
    return make_text_result(
        "\n".join(
            [
                "Agent registered.",
                f"agentId: {agent_id}",
                f"role: {role}",
                f"status: {status}",
                f"projectIds: {', '.join(project_ids) if project_ids else '<all>'}",
            ]
        )
    )


def tool_list_agents(arguments: dict[str, Any]) -> dict[str, Any]:
    role_filter = str(arguments.get("role") or "").strip()
    project_filter = normalize_id(arguments, "projectId", "project_id", required=False)
    registry = read_registry()
    agents = []
    for agent_id, agent in sorted((registry.get("agents") or {}).items()):
        if role_filter and agent.get("role") != role_filter:
            continue
        project_ids = list(agent.get("projectIds") or [])
        if project_filter and project_ids and project_filter not in project_ids:
            continue
        agents.append({"agentId": agent_id, **agent})
    if not agents:
        return make_text_result("No agents registered.")
    return make_text_result(json.dumps(agents, ensure_ascii=False, indent=2))


def update_registry_job(job_id: str, updates: dict[str, Any]) -> None:
    registry = read_registry()
    jobs = registry.setdefault("jobs", {})
    job = dict(jobs.get(job_id) or {})
    job.update(updates)
    job["updated_at"] = utc_now()
    jobs[job_id] = job
    write_registry(registry)


def tool_notify_task_ready(arguments: dict[str, Any]) -> dict[str, Any]:
    project = resolve_project(arguments)
    if not project:
        raise ValueError("projectId is required")
    project_id = str(project["projectId"])
    configure_project(project)
    task_ids = normalize_task_ids(arguments)
    files = normalize_string_list(arguments.get("files"), "files")
    message = str(arguments.get("message") or "").strip()
    signal_id = f"task-ready-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    signal_path = SIGNALS_ROOT / f"{signal_id}.json"

    missing_task_ids: list[str] = []
    job_records: list[dict[str, Any]] = []
    registry = read_registry()
    jobs = registry.setdefault("jobs", {})
    for task_id in task_ids:
        try:
            job_id, job_paths = create_workflow_job(
                task_id,
                {
                    **arguments,
                    "task_id": task_id,
                    "files": files,
                    "trigger": "notify_task_ready",
                },
            )
        except ValueError:
            missing_task_ids.append(task_id)
            continue

        update_job_status(
            job_id,
            projectId=project_id,
            signal_id=signal_id,
            trigger="notify_task_ready",
            dispatch_status="pending",
            gemini_started=False,
            external_data_approved=False,
            codex_sent_task_content_to_mcp=False,
            mcp_embedded_task_content_in_prompt=False,
        )
        job_status = read_json(job_paths["status"])
        record = {
            "job_id": job_id,
            "projectId": project_id,
            "taskId": task_id,
            "status": "queued",
            "dispatch_status": "pending",
            "workspace": str(REPO_ROOT),
            "workflow_dir": AI_WORKFLOW_RELATIVE_ROOT.as_posix(),
            "task_file": job_status.get("task_file"),
            "status_file": str(job_paths["status"].relative_to(REPO_ROOT)),
            "status_file_abs": str(job_paths["status"]),
            "signal_id": signal_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        jobs[job_id] = record
        job_records.append(record)

    if not job_records:
        raise ValueError(f"No runnable task files found for: {', '.join(task_ids)}")

    payload = {
        "signal_id": signal_id,
        "type": "task_ready",
        "status": "accepted",
        "created_at": utc_now(),
        "projectId": project_id,
        "workspace_root": str(REPO_ROOT),
        "workflow_root": str(AI_WORKFLOW_ROOT.relative_to(REPO_ROOT)),
        "taskId": task_ids[0] if len(task_ids) == 1 else None,
        "taskIds": task_ids,
        "task_files": [str(record.get("task_file")) for record in job_records if record.get("task_file")],
        "missing_task_ids": missing_task_ids,
        "jobs": job_records,
        "files": files,
        "message": message,
        "gemini_started": False,
        "dispatch_required": True,
    }
    write_json_atomic(signal_path, payload)
    write_registry(registry)

    lines = [
        "Task ready signal accepted.",
        "Gemini was not started.",
        f"projectId: {project_id}",
        f"signal_id: {signal_id}",
        f"signal_file: {signal_path.relative_to(REPO_ROOT)}",
        f"job_ids: {', '.join(record['job_id'] for record in job_records)}",
    ]
    if missing_task_ids:
        lines.append(f"missing_task_ids: {', '.join(missing_task_ids)}")
    return make_text_result("\n".join(lines))


def tool_list_platform_jobs(arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = normalize_id(arguments, "projectId", "project_id", required=False)
    status_filter = str(arguments.get("status") or "").strip()
    limit = int(arguments.get("limit") or 20)
    registry = read_registry()
    jobs = []
    for raw_job in (registry.get("jobs") or {}).values():
        job = dict(raw_job)
        status_file = job.get("status_file_abs")
        if status_file:
            status = read_json(resolve_path(str(status_file)))
            if status:
                job["workflow_status"] = status.get("status")
                job["returncode"] = status.get("returncode")
                job["finished_at"] = status.get("finished_at")
        jobs.append(job)
    if project_id:
        jobs = [job for job in jobs if job.get("projectId") == project_id]
    if status_filter:
        jobs = [
            job
            for job in jobs
            if job.get("status") == status_filter or job.get("dispatch_status") == status_filter
        ]
    jobs.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return make_text_result(json.dumps(jobs[:limit], ensure_ascii=False, indent=2))


def tool_notify_workflow_ready(arguments: dict[str, Any]) -> dict[str, Any]:
    task_ids = [
        *normalize_string_list(arguments.get("task_ids"), "task_ids"),
        *normalize_string_list(arguments.get("taskIds"), "taskIds"),
    ]
    single_task_id = str(arguments.get("task_id") or arguments.get("taskId") or "").strip()
    if single_task_id:
        task_ids = [single_task_id, *[item for item in task_ids if item != single_task_id]]
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        raise ValueError("task_id or task_ids is required")

    files = normalize_string_list(arguments.get("files"), "files")
    message = str(arguments.get("message", "")).strip()
    signal_id = f"workflow-ready-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    signal_path = SIGNALS_ROOT / f"{signal_id}.json"

    missing_task_ids: list[str] = []
    job_records: list[dict[str, Any]] = []
    for task_id in task_ids:
        try:
            job_arguments = {
                **arguments,
                "task_id": task_id,
                "signal_id": signal_id,
                "trigger": "notify_workflow_ready",
            }
            job_id, job_paths = create_workflow_job(task_id, job_arguments)
        except ValueError:
            missing_task_ids.append(task_id)
            continue

        update_job_status(
            job_id,
            signal_id=signal_id,
            trigger="notify_workflow_ready",
            prompt_mode="task_path",
        )
        pid = spawn_workflow_worker(job_id)
        job_status = read_json(job_paths["status"])
        job_records.append(
            {
                "taskId": task_id,
                "job_id": job_id,
                "launcher_pid": pid,
                "task_file": job_status.get("task_file"),
                "status_file": str(job_paths["status"].relative_to(REPO_ROOT)),
            }
        )

    if not job_records:
        raise ValueError(f"No runnable task files found for: {', '.join(task_ids)}")

    payload: dict[str, Any] = {
        "signal_id": signal_id,
        "type": "workflow_ready",
        "status": "accepted",
        "created_at": utc_now(),
        "workspace_root": str(REPO_ROOT),
        "workflow_root": str(AI_WORKFLOW_ROOT.relative_to(REPO_ROOT)),
        "taskId": task_ids[0] if len(task_ids) == 1 else None,
        "taskIds": task_ids,
        "task_ids": task_ids,
        "task_files": [str(record.get("task_file")) for record in job_records if record.get("task_file")],
        "missing_task_ids": missing_task_ids,
        "jobs": job_records,
        "files": files,
        "message": message,
        "codex_sent_task_content_to_mcp": False,
        "mcp_embedded_task_content_in_prompt": False,
        "gemini_started": True,
        "gemini_prompt_mode": "task_path",
    }
    write_json_atomic(signal_path, payload)

    lines = [
        "Workflow ready signal accepted.",
        "Task contents were not sent through MCP.",
        "Gemini background job(s) were started with task document path prompts.",
        f"signal_id: {signal_id}",
        f"workspace: {REPO_ROOT}",
        f"workflow_root: {AI_WORKFLOW_ROOT.relative_to(REPO_ROOT)}",
        f"signal_file: {signal_path.relative_to(REPO_ROOT)}",
        f"task_ids: {', '.join(task_ids)}",
        f"job_ids: {', '.join(str(record['job_id']) for record in job_records)}",
    ]
    if missing_task_ids:
        lines.append(f"missing_task_ids: {', '.join(missing_task_ids)}")
    return make_text_result("\n".join(lines))


def create_workflow_job(task_id: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Path]]:
    paths = get_workflow_paths(task_id)
    if paths["todo"].exists():
        paths["working"].parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(paths["todo"]), str(paths["working"]))
    elif not paths["working"].exists():
        raise ValueError(f"Task file not found: {paths['todo']}")

    job_id = f"{task_id}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    job_paths = get_job_paths(job_id)
    job_paths["dir"].mkdir(parents=True, exist_ok=False)
    shutil.copy2(paths["working"], job_paths["task"])
    files = normalize_string_list(arguments.get("files"), "files")

    status = {
        "job_id": job_id,
        "taskId": task_id,
        "task_id": task_id,
        "status": "queued",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "workspace_root": str(REPO_ROOT),
        "workflow_root": str(AI_WORKFLOW_ROOT.relative_to(REPO_ROOT)),
        "task_file": str(paths["working"].relative_to(REPO_ROOT)),
        "job_task_file": str(job_paths["task"].relative_to(REPO_ROOT)),
        "stdout_file": str(job_paths["stdout"].relative_to(REPO_ROOT)),
        "stderr_file": str(job_paths["stderr"].relative_to(REPO_ROOT)),
        "events_file": str(job_paths["events"].relative_to(REPO_ROOT)),
        "result_file": str(job_paths["result"].relative_to(REPO_ROOT)),
        "patch_file": str(job_paths["patch"].relative_to(REPO_ROOT)),
        "files": files,
        "prompt_mode": "task_path",
        "arguments": {
            key: value
            for key, value in arguments.items()
            if key
            not in {
                "task_id",
                "taskId",
                "task_ids",
                "taskIds",
                "projectId",
                "project_id",
                "workspace",
                "files",
                "message",
                "signal_id",
                "trigger",
            }
        },
    }
    write_json_atomic(job_paths["status"], status)
    append_job_event(job_id, "job_created", {"task_id": task_id})
    return job_id, job_paths


def spawn_workflow_worker(job_id: str) -> int:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--workflow-worker", job_id]
    worker_env = os.environ.copy()
    worker_env["GEMINI_BRIDGE_WORKSPACE"] = str(REPO_ROOT)
    worker_env["GEMINI_BRIDGE_WORKFLOW_DIR"] = AI_WORKFLOW_RELATIVE_ROOT.as_posix()
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=worker_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    update_job_status(job_id, status="queued", launcher_pid=proc.pid, worker_command=cmd)
    append_job_event(job_id, "worker_spawned", {"launcher_pid": proc.pid, "command": cmd})
    return proc.pid


def tool_start_workflow_job(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id", "")).strip()
    if not task_id:
        raise ValueError("task_id is required")

    job_id, job_paths = create_workflow_job(task_id, arguments)
    pid = spawn_workflow_worker(job_id)
    lines = [
        "Workflow job started.",
        f"job_id: {job_id}",
        f"launcher_pid: {pid}",
        f"workspace: {REPO_ROOT}",
        f"workflow_root: {AI_WORKFLOW_ROOT.relative_to(REPO_ROOT)}",
        f"status: {job_paths['status'].relative_to(REPO_ROOT)}",
        f"events: {job_paths['events'].relative_to(REPO_ROOT)}",
        f"stdout: {job_paths['stdout'].relative_to(REPO_ROOT)}",
        f"stderr: {job_paths['stderr'].relative_to(REPO_ROOT)}",
        f"result: {job_paths['result'].relative_to(REPO_ROOT)}",
        f"patch: {job_paths['patch'].relative_to(REPO_ROOT)}",
    ]
    return make_text_result("\n".join(lines))


def tool_get_workflow_job(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = str(arguments.get("job_id", "")).strip()
    if not job_id:
        raise ValueError("job_id is required")
    paths = get_job_paths(job_id)
    if not paths["status"].exists():
        return make_text_result(f"Job not found: {job_id}", is_error=True)

    status = read_json(paths["status"])
    lines = [
        f"job_id: {job_id}",
        f"status: {status.get('status')}",
        f"task_id: {status.get('task_id')}",
        f"workspace: {status.get('workspace_root', REPO_ROOT)}",
        f"workflow_root: {status.get('workflow_root', AI_WORKFLOW_ROOT.relative_to(REPO_ROOT))}",
        f"created_at: {status.get('created_at')}",
        f"updated_at: {status.get('updated_at')}",
        f"returncode: {status.get('returncode', '<none>')}",
        f"status_file: {paths['status'].relative_to(REPO_ROOT)}",
        f"events_file: {paths['events'].relative_to(REPO_ROOT)}",
        f"stdout_file: {paths['stdout'].relative_to(REPO_ROOT)}",
        f"stderr_file: {paths['stderr'].relative_to(REPO_ROOT)}",
        f"result_file: {paths['result'].relative_to(REPO_ROOT)}",
        f"patch_file: {paths['patch'].relative_to(REPO_ROOT)}",
    ]
    if arguments.get("include_tail"):
        lines.extend(
            [
                "",
                "--- events tail ---",
                tail_file(paths["events"], 3000) or "<empty>",
                "",
                "--- stdout tail ---",
                tail_file(paths["stdout"], 3000) or "<empty>",
                "",
                "--- stderr tail ---",
                tail_file(paths["stderr"], 3000) or "<empty>",
            ]
        )
    return make_text_result("\n".join(lines))


def tool_list_workflow_jobs(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = int(arguments.get("limit") or 20)
    if not JOBS_ROOT.exists():
        return make_text_result("No workflow jobs found.")
    statuses = []
    for status_path in sorted(JOBS_ROOT.glob("*/status.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            statuses.append(read_json(status_path))
        except (OSError, json.JSONDecodeError):
            continue
        if len(statuses) >= limit:
            break
    if not statuses:
        return make_text_result("No workflow jobs found.")
    lines = []
    for status in statuses:
        lines.append(
            " | ".join(
                [
                    str(status.get("job_id")),
                    str(status.get("status")),
                    f"task={status.get('task_id')}",
                    f"updated={status.get('updated_at')}",
                ]
            )
        )
    return make_text_result("\n".join(lines))


def tool_cancel_workflow_job(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = str(arguments.get("job_id", "")).strip()
    if not job_id:
        raise ValueError("job_id is required")
    paths = get_job_paths(job_id)
    if not paths["status"].exists():
        return make_text_result(f"Job not found: {job_id}", is_error=True)

    paths["cancel"].write_text(utc_now() + "\n", encoding="utf-8")
    status = read_json(paths["status"])
    pid = status.get("gemini_pid") or status.get("worker_pid") or status.get("launcher_pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            append_job_event(job_id, "cancel_signal_failed", {"pid": pid, "error": str(exc)})
    update_job_status(job_id, status="cancelling", cancel_requested_at=utc_now())
    append_job_event(job_id, "cancel_requested", {"pid": pid})
    return make_text_result(f"Cancellation requested for job {job_id}.")


def tool_apply_workflow_patch(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id", "")).strip()
    paths = get_workflow_paths(task_id)
    if not paths["patch"].exists():
        return make_text_result(f"Patch file not found: {paths['patch']}", is_error=True)
    
    try:
        proc = subprocess.run(
            ["git", "apply", str(paths["patch"])],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False
        )
        if proc.returncode == 0:
            return make_text_result(f"Patch for {task_id} applied successfully.")
        else:
            return make_text_result(f"Failed to apply patch:\n{proc.stderr}", is_error=True)
    except Exception as exc:
        return make_text_result(f"Error applying patch: {exc}", is_error=True)


def tool_run_workflow_tests(arguments: dict[str, Any]) -> dict[str, Any]:
    test_command = str(arguments.get("test_command", "")).strip()
    if not test_command:
        if (REPO_ROOT / "package.json").exists():
            test_command = "npm test"
        elif (REPO_ROOT / "pytest.ini").exists() or (REPO_ROOT / "tests").exists():
            test_command = "pytest"
        else:
            return make_text_result("Could not detect test runner. Please provide test_command.", is_error=True)

    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False
        )
        status = "passed" if proc.returncode == 0 else "failed"
        return make_text_result(f"Tests {status}.\n\nSTDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}")
    except Exception as exc:
        return make_text_result(f"Error running tests: {exc}", is_error=True)


def tool_finalize_workflow_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id", "")).strip()
    summary = str(arguments.get("summary", "")).strip()
    status = str(arguments.get("status", "success")).strip()
    
    paths = get_workflow_paths(task_id)
    if not paths["working"].exists():
        return make_text_result(f"Task {task_id} not in working state.", is_error=True)
    
    # Write report
    report_content = [
        f"# Task Report: {task_id}",
        f"Status: {status}",
        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary",
        summary,
        "",
        "## Files",
        f"- Task: {paths['done'].relative_to(REPO_ROOT)}",
        f"- Output: {paths['output'].relative_to(REPO_ROOT)}",
        f"- Patch: {paths['patch'].relative_to(REPO_ROOT)}",
    ]
    paths["report"].parent.mkdir(parents=True, exist_ok=True)
    paths["report"].write_text("\n".join(report_content), encoding="utf-8")
    
    # Move to done
    paths["done"].parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(paths["working"]), str(paths["done"]))
    
    return make_text_result(f"Task {task_id} finalized. Report written to {paths['report'].relative_to(REPO_ROOT)}")


WORKFLOW_TOOL_NAMES = {
    "get_workflow_job",
    "cancel_workflow_job",
}

PLATFORM_TOOL_NAMES = ASYNC_WORKER_TOOL_NAMES - WORKFLOW_TOOL_NAMES


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "notifications/initialized":
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
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
        params = message.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        
        try:
            if name not in ASYNC_WORKER_TOOL_NAMES:
                return jsonrpc_error(msg_id, -32602, f"Unknown tool: {name}")
            if name in WORKFLOW_TOOL_NAMES:
                configure_workspace(arguments.get("workspace"))
            if name == "register_project":
                result = tool_register_project(arguments)
            elif name == "list_projects":
                result = tool_list_projects(arguments)
            elif name == "get_project":
                result = tool_get_project(arguments)
            elif name == "unregister_project":
                result = tool_unregister_project(arguments)
            elif name == "register_agent":
                result = tool_register_agent(arguments)
            elif name == "list_agents":
                result = tool_list_agents(arguments)
            elif name == "notify_task_ready":
                result = tool_notify_task_ready(arguments)
            elif name == "list_platform_jobs":
                result = tool_list_platform_jobs(arguments)
            elif name == "get_workflow_job":
                result = tool_get_workflow_job(arguments)
            elif name == "cancel_workflow_job":
                result = tool_cancel_workflow_job(arguments)
            else:
                return jsonrpc_error(msg_id, -32602, f"Unknown tool: {name}")
        except ValueError as exc:
            return jsonrpc_error(msg_id, -32602, str(exc))
        except Exception as exc:
            return jsonrpc_error(msg_id, -32603, f"Internal error: {exc}\n{traceback.format_exc()}")
            
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    if msg_id is None:
        return None

    return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


def jsonrpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        try:
            response = handle_request(message)
        except Exception as exc:  # pragma: no cover
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {
                    "code": -32603,
                    "message": str(exc),
                    "data": traceback.format_exc(),
                },
            }
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--workflow-worker":
        raise SystemExit(workflow_job_worker(sys.argv[2]))
    main()
