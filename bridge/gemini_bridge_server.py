#!/usr/bin/env python3
"""Local HTTP bridge server that proxies requests to the real Gemini CLI."""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import sys
import threading
import time
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW_RELATIVE_ROOT = Path("docs") / "ai-workflow"
DEFAULT_REGISTRY_RELATIVE_PATH = Path("gemini-bridge") / "registry.json"
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


def resolve_workspace_root() -> Path:
    raw = os.getenv("GEMINI_BRIDGE_WORKSPACE") or os.getenv("CODEX_WORKSPACE")
    if raw:
        root = Path(str(raw)).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"workspace does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        return root

    cwd = Path.cwd().resolve()
    if (cwd / ".git").exists() or (cwd / "package.json").exists() or (cwd / "pyproject.toml").exists():
        return cwd

    return DEFAULT_REPO_ROOT


REPO_ROOT = resolve_workspace_root()


def default_worker_script() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins" / "gemini-bridge" / "scripts" / "gemini_bridge_mcp.py"


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in {"true", "1", "yes", "on"}


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def default_child_path() -> str:
    paths: list[str] = []
    seen: set[str] = set()

    home = Path.home().resolve()
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


@dataclass
class BridgeConfig:
    host: str = os.getenv("GEMINI_BRIDGE_HOST", "127.0.0.1")
    port: int = env_int("GEMINI_BRIDGE_PORT", 8787)
    gemini_bin: str = os.getenv("GEMINI_BIN", "gemini")
    default_timeout: int = env_int("GEMINI_TIMEOUT_SECONDS", 360)
    daemon_enabled: bool = env_bool("GEMINI_BRIDGE_DAEMON_ENABLED", True)
    daemon_poll_seconds: float = env_float("GEMINI_BRIDGE_DAEMON_POLL_SECONDS", 5.0)
    daemon_max_jobs_per_tick: int = env_int("GEMINI_BRIDGE_DAEMON_MAX_JOBS_PER_TICK", 1)
    daemon_max_retries: int = env_int("GEMINI_BRIDGE_DAEMON_MAX_RETRIES", 3)
    daemon_retry_delay: float = env_float("GEMINI_BRIDGE_DAEMON_RETRY_DELAY", 10.0)
    worker_script: Path = Path(os.getenv("GEMINI_BRIDGE_WORKER_SCRIPT", str(default_worker_script()))).expanduser().resolve()
    base_args: tuple[str, ...] = tuple(
        shlex.split(os.getenv("GEMINI_BASE_ARGS", ""))
    )


CONFIG = BridgeConfig()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def registry_file_path() -> Path:
    configured = os.getenv("GEMINI_BRIDGE_REGISTRY_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = os.getenv("CODEX_HOME")
    root = Path(str(codex_home)).expanduser().resolve() if codex_home else Path.home().resolve() / ".codex"
    return root / DEFAULT_REGISTRY_RELATIVE_PATH


def read_registry() -> dict[str, Any]:
    path = registry_file_path()
    if not path.exists():
        return {"version": 1, "created_at": utc_now(), "updated_at": utc_now(), "projects": {}, "agents": {}, "jobs": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "projects": {}, "agents": {}, "jobs": {}}


def write_registry(registry: dict[str, Any]) -> None:
    path = registry_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = utc_now()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def update_registry_job(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    registry = read_registry()
    jobs = registry.setdefault("jobs", {})
    job = dict(jobs.get(job_id) or {})
    job.update(updates)
    jobs[job_id] = job
    write_registry(registry)
    return job


def platform_job_status_path(job: dict[str, Any]) -> Path:
    workspace = Path(str(job.get("workspace") or REPO_ROOT)).expanduser().resolve()
    workflow_dir = str(job.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())
    job_id = str(job.get("job_id") or "")
    return workspace / workflow_dir / "jobs" / job_id / "status.json"


def platform_job_events_path(job: dict[str, Any]) -> Path:
    workspace = Path(str(job.get("workspace") or REPO_ROOT)).expanduser().resolve()
    workflow_dir = str(job.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())
    job_id = str(job.get("job_id") or "")
    return workspace / workflow_dir / "jobs" / job_id / "events.log"


def platform_job_stdout_path(job: dict[str, Any]) -> Path:
    workspace = Path(str(job.get("workspace") or REPO_ROOT)).expanduser().resolve()
    workflow_dir = str(job.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())
    job_id = str(job.get("job_id") or "")
    return workspace / workflow_dir / "jobs" / job_id / "stdout.log"


def platform_job_stderr_path(job: dict[str, Any]) -> Path:
    workspace = Path(str(job.get("workspace") or REPO_ROOT)).expanduser().resolve()
    workflow_dir = str(job.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())
    job_id = str(job.get("job_id") or "")
    return workspace / workflow_dir / "jobs" / job_id / "stderr.log"


def platform_job_result_path(job: dict[str, Any]) -> Path:
    workspace = Path(str(job.get("workspace") or REPO_ROOT)).expanduser().resolve()
    workflow_dir = str(job.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())
    job_id = str(job.get("job_id") or "")
    return workspace / workflow_dir / "jobs" / job_id / "result.md"


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def tail_file(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]
    except Exception:
        return ""


def update_platform_job_status(job: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    path = platform_job_status_path(job)
    status = read_json_file(path)
    status.update(updates)
    status["updated_at"] = utc_now()
    write_json_file(path, status)
    return status


def append_platform_job_event(job: dict[str, Any], event: str, payload: dict[str, Any] | None = None) -> None:
    path = platform_job_events_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"time": utc_now(), "event": event, **(payload or {})}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def list_platform_jobs(limit: int = 500) -> list[dict[str, Any]]:
    registry = read_registry()
    jobs_map = registry.get("jobs") or {}
    all_jobs = list(jobs_map.values())
    all_jobs.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    return all_jobs[:limit]


def platform_job_detail(job_id: str) -> dict[str, Any] | None:
    registry = read_registry()
    job = (registry.get("jobs") or {}).get(job_id)
    if not job:
        return None

    status_path = platform_job_status_path(job)
    events_path = platform_job_events_path(job)
    stdout_path = platform_job_stdout_path(job)
    stderr_path = platform_job_stderr_path(job)
    result_path = platform_job_result_path(job)

    return {
        "job": job,
        "status": read_json_file(status_path),
        "events_tail": tail_file(events_path),
        "stdout_tail": tail_file(stdout_path),
        "stderr_tail": tail_file(stderr_path),
        "result_tail": tail_file(result_path),
    }


class GeminiJobDaemon:
    def __init__(self) -> None:
        self.enabled = CONFIG.daemon_enabled
        self.lock = threading.Lock()
        self.last_scan_at: str | None = None
        self.last_dispatch_at: str | None = None
        self.last_error: str | None = None
        self.dispatched_count = 0
        self.thread: threading.Thread | None = None

    def start_thread(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run_loop, daemon=True, name="GeminiJobDaemon")
        self.thread.start()

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.enabled = enabled

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.enabled,
                "thread_alive": self.thread.is_alive() if self.thread else False,
                "poll_seconds": CONFIG.daemon_poll_seconds,
                "max_jobs_per_tick": CONFIG.daemon_max_jobs_per_tick,
                "pending_jobs": len([j for j in list_platform_jobs(500) if self.is_dispatchable(j)]),
                "last_scan_at": self.last_scan_at,
                "last_dispatch_at": self.last_dispatch_at,
                "last_error": self.last_error,
                "dispatched_count": self.dispatched_count,
                "worker_script": str(CONFIG.worker_script),
            }

    @staticmethod
    def is_dispatchable(job: dict[str, Any]) -> bool:
        dispatch_status = str(job.get("dispatch_status") or "").strip()
        registry_status = str(job.get("status") or "").strip()
        workflow_status = str(job.get("workflow_status") or "").strip()
        
        # Standard case: newly queued job
        if (dispatch_status in {"", "pending"}
            and registry_status in {"", "queued"}
            and workflow_status in {"", "queued"}):
            return True
            
        # Retry case: failed job with remaining retry attempts
        if registry_status == "failed" or workflow_status == "failed":
            retry_count = int(job.get("retry_count") or 0)
            if retry_count < CONFIG.daemon_max_retries:
                # Check if 10s has passed since last update to avoid rapid looping
                updated_at_str = str(job.get("updated_at") or "")
                if updated_at_str:
                    try:
                        updated_at = datetime.fromisoformat(updated_at_str)
                        # Ensure comparison is timezone-aware
                        now = datetime.now(timezone.utc)
                        if (now - updated_at).total_seconds() >= CONFIG.daemon_retry_delay:
                            return True
                    except ValueError:
                        return True
        
        return False

    def dispatch_pending_jobs(self, limit: int = 1) -> list[dict[str, Any]]:
        dispatched: list[dict[str, Any]] = []
        with self.lock:
            self.last_scan_at = utc_now()
            for job in list_platform_jobs(limit=500):
                if len(dispatched) >= limit:
                    break
                if not self.is_dispatchable(job):
                    continue
                dispatched.append(self.dispatch_job(job))
        return dispatched

    def dispatch_job_by_id(self, job_id: str) -> dict[str, Any]:
        registry = read_registry()
        job = dict((registry.get("jobs") or {}).get(job_id) or {})
        if not job:
            raise BridgeError(f"Platform job not found: {job_id}", HTTPStatus.NOT_FOUND)
        with self.lock:
            # For manual dispatch, we allow re-dispatching even if status is not 'pending' or 'failed'
            # This acts as a manual override/reset.
            return self.dispatch_job(job)

    def dispatch_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise BridgeError("Job is missing job_id")

        status_path = platform_job_status_path(job)
        if not status_path.exists():
            update_registry_job(job_id, {"status": "failed", "dispatch_status": "failed", "error": f"status file not found: {status_path}"})
            raise BridgeError(f"Job status file not found: {status_path}", HTTPStatus.NOT_FOUND)

        workspace = Path(str(job.get("workspace") or REPO_ROOT)).expanduser().resolve()
        workflow_dir = str(job.get("workflow_dir") or DEFAULT_WORKFLOW_RELATIVE_ROOT.as_posix())
        worker_env = os.environ.copy()
        worker_env["GEMINI_BRIDGE_WORKSPACE"] = str(workspace)
        worker_env["GEMINI_BRIDGE_WORKFLOW_DIR"] = workflow_dir
        worker_env["GEMINI_BIN"] = CONFIG.gemini_bin
        worker_env["PATH"] = default_child_path()
        worker_env.setdefault("GEMINI_CLI_HOME", worker_env.get("HOME", str(Path.home().resolve())))

        cmd = [sys.executable, str(CONFIG.worker_script), "--workflow-worker", job_id]
        
        # Increment retry count if this is a retry
        old_status = str(job.get("status") or "")
        current_retry = int(job.get("retry_count") or 0)
        new_retry_count = current_retry + 1 if old_status == "failed" else current_retry
        
        dispatch_updates = {
            "trigger": "local_daemon",
            "dispatch_status": "dispatching",
            "daemon_dispatch_started_at": utc_now(),
            "external_data_approved": True,
            "external_data_approved_by": "local_daemon",
            "gemini_started": True,
            "retry_count": new_retry_count
        }
        update_platform_job_status(job, dispatch_updates)
        update_registry_job(job_id, {**job, "status": "dispatching", **dispatch_updates})
        
        event_name = "daemon_dispatch_retry" if old_status == "failed" else "daemon_dispatch_started"
        append_platform_job_event(job, event_name, {"command": cmd, "workspace": str(workspace), "retry_attempt": new_retry_count})

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=worker_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except Exception as exc:
            failed_updates = {
                "status": "failed",
                "dispatch_status": "failed",
                "error": f"failed to spawn worker: {exc}",
                "finished_at": utc_now(),
            }
            update_platform_job_status(job, failed_updates)
            update_registry_job(job_id, {**job, **failed_updates})
            append_platform_job_event(job, "daemon_dispatch_failed", {"error": str(exc)})
            raise

        launched_updates = {
            "dispatch_status": "dispatched",
            "launcher_pid": proc.pid,
            "worker_command": cmd,
            "daemon_dispatched_at": utc_now(),
        }
        update_platform_job_status(job, launched_updates)
        updated_job = update_registry_job(
            job_id,
            {
                **job,
                "status": "dispatched",
                "dispatch_status": "dispatched",
                "external_data_approved": True,
                "external_data_approved_by": "local_daemon",
                "gemini_started": True,
                "launcher_pid": proc.pid,
                "worker_command": cmd,
                "daemon_dispatched_at": launched_updates["daemon_dispatched_at"],
                "retry_count": new_retry_count
            },
        )
        append_platform_job_event(job, "daemon_worker_spawned", {"launcher_pid": proc.pid, "command": cmd})
        self.last_dispatch_at = launched_updates["daemon_dispatched_at"]
        self.last_error = None
        self.dispatched_count += 1
        return updated_job

    def run_loop(self) -> None:
        while True:
            try:
                enabled = False
                with self.lock:
                    enabled = self.enabled
                
                if enabled:
                    self.dispatch_pending_jobs(limit=CONFIG.daemon_max_jobs_per_tick)
                else:
                    with self.lock:
                        self.last_scan_at = utc_now()
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
            
            time.sleep(CONFIG.daemon_poll_seconds)


DAEMON = GeminiJobDaemon()


def dashboard_html() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NexusFlow Console Pro</title>
  <style>
    :root {{
      --primary: #0f172a;
      --accent: #0d9488;
      --accent-light: #f0fdfa;
      --bg: #f8fafc;
      --sidebar: #ffffff;
      --card: #ffffff;
      --border: #e2e8f0;
      --text-main: #1e293b;
      --text-muted: #64748b;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --info: #3b82f6;
      --processing: #eab308;
      --pending: #94a3b8;
      --font: "Inter", system-ui, -apple-system, sans-serif;
    }}
    
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      font-family: var(--font);
      background-color: var(--bg);
      color: var(--text-main);
      overflow: hidden;
    }}

    /* Header Styling */
    header {{
      flex: 0 0 64px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0 24px;
      background: var(--primary);
      color: white;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      z-index: 100;
    }}
    .logo {{ display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 18px; letter-spacing: -0.025em; }}
    .logo-icon {{ width: 32px; height: 32px; background: var(--accent); border-radius: 8px; display: flex; align-items: center; justify-content: center; }}
    
    .header-actions {{ display: flex; align-items: center; gap: 16px; }}
    .daemon-badge {{
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.1);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .status-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
    .status-dot.active {{ background: var(--success); box-shadow: 0 0 8px var(--success); }}
    .status-dot.inactive {{ background: var(--danger); }}

    /* Layout Structure */
    main {{ flex: 1; display: flex; overflow: hidden; }}

    /* Left Sidebar: Projects */
    .sidebar {{
      flex: 0 0 240px;
      background: var(--sidebar);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      padding-top: 16px;
    }}
    .sidebar-title {{ padding: 0 20px 12px; font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }}
    .project-list {{ flex: 1; overflow-y: auto; padding: 0 12px; }}
    .project-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 12px;
      margin-bottom: 4px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
      color: var(--text-main);
      transition: all 0.2s;
    }}
    .project-item:hover {{ background: #f1f5f9; }}
    .project-item.active {{ background: var(--accent-light); color: var(--accent); }}
    .project-badge {{ font-size: 11px; background: #e2e8f0; padding: 2px 6px; border-radius: 6px; color: var(--text-muted); }}
    .project-item.active .project-badge {{ background: var(--accent); color: white; }}

    /* Middle Column: Task Queue */
    .queue-pane {{
      flex: 0 0 360px;
      background: #f8fafc;
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
    }}
    .pane-header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      background: white;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .pane-title {{ font-size: 16px; font-weight: 700; }}
    
    .toolbar {{
      padding: 12px 16px;
      background: white;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    
    button {{
      padding: 6px 12px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: white;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      color: var(--text-main);
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    button:hover {{ background: #f8fafc; border-color: #cbd5e1; }}
    button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
    button.primary:hover {{ opacity: 0.9; }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}

    .task-list {{ flex: 1; overflow-y: auto; padding: 16px; }}
    .task-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 12px;
      cursor: pointer;
      transition: all 0.2s;
      position: relative;
      overflow: hidden;
    }}
    .task-card:hover {{ border-color: var(--accent); box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }}
    .task-card.active {{ border-color: var(--accent); border-width: 2px; padding: 13px; background: var(--accent-light); }}
    .task-card-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }}
    .task-name {{ font-weight: 700; font-size: 14px; color: var(--text-main); word-break: break-all; }}
    .task-time {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; }}
    
    .pill {{
      font-size: 10px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.025em;
    }}
    .pill.gray {{ background: #f1f5f9; color: #64748b; }}
    .pill.blue {{ background: #e0f2fe; color: #0284c7; }}
    .pill.green {{ background: #dcfce7; color: #059669; }}
    .pill.red {{ background: #fee2e2; color: #dc2626; }}
    .pill.orange {{ background: #ffedd5; color: #d97706; }}

    /* Right Column: Details */
    .detail-pane {{ flex: 1; background: white; display: flex; flex-direction: column; }}
    .detail-header {{ padding: 20px 24px; border-bottom: 1px solid var(--border); }}
    .detail-title-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
    
    .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }}
    .stat-item {{ padding: 12px; background: #f8fafc; border-radius: 8px; border: 1px solid var(--border); }}
    .stat-label {{ font-size: 11px; color: var(--text-muted); font-weight: 600; margin-bottom: 4px; text-transform: uppercase; }}
    .stat-value {{ font-size: 14px; font-weight: 700; }}

    .tabs {{ display: flex; padding: 0 24px; border-bottom: 1px solid var(--border); background: #f8fafc; }}
    .tab {{
      padding: 14px 16px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      border-bottom: 2px solid transparent;
      transition: all 0.2s;
    }}
    .tab:hover {{ color: var(--accent); }}
    .tab.active {{ color: var(--accent); border-bottom-color: var(--accent); background: white; }}

    .tab-body {{ flex: 1; overflow-y: auto; padding: 24px; }}

    /* Code & Timeline Viewers */
    .code-block {{
      background: #1e293b;
      color: #e2e8f0;
      padding: 16px;
      border-radius: 12px;
      font-family: 'JetBrains Mono', 'Fira Code', monospace;
      font-size: 13px;
      line-height: 1.6;
      overflow-x: auto;
      margin: 0;
      white-space: pre-wrap;
      word-break: break-all;
    }}

    .timeline {{ position: relative; padding-left: 36px; padding-top: 8px; margin-top: 8px; }}
    .timeline::before {{ content: ''; position: absolute; left: 15px; top: 0; bottom: 0; width: 2px; background: #e2e8f0; }}
    .timeline-item {{ position: relative; margin-bottom: 24px; }}
    .timeline-marker {{
      position: absolute;
      left: -36px;
      top: 0;
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: white;
      border: 2px solid #e2e8f0;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1;
      box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }}
    .timeline-marker svg {{ width: 14px; height: 14px; color: #64748b; }}
    .timeline-item.success .timeline-marker {{ border-color: var(--success); background: var(--success); }}
    .timeline-item.success .timeline-marker svg {{ color: white; }}
    .timeline-item.error .timeline-marker {{ border-color: var(--danger); background: var(--danger); }}
    .timeline-item.error .timeline-marker svg {{ color: white; }}
    .timeline-item.info .timeline-marker {{ border-color: var(--info); background: #eff6ff; }}
    .timeline-item.info .timeline-marker svg {{ color: var(--info); }}
    .timeline-item.processing .timeline-marker {{ border-color: var(--processing); background: var(--processing); }}
    .timeline-item.processing .timeline-marker svg {{ color: white; }}
    
    .timeline-content {{
      background: white;
      padding: 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      box-shadow: 0 1px 3px rgba(0,0,0,0.02);
    }}
    .timeline-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
    .timeline-title {{ font-weight: 700; font-size: 14px; color: var(--text-main); }}
    .timeline-time {{ font-size: 12px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }}
    
    .timeline-payload {{
      background: #f8fafc;
      border-radius: 8px;
      padding: 12px;
      border: 1px solid #f1f5f9;
    }}
    .payload-row {{ display: flex; margin-bottom: 6px; font-size: 12px; }}
    .payload-row:last-child {{ margin-bottom: 0; }}
    .payload-key {{ width: 140px; flex-shrink: 0; color: var(--text-muted); font-weight: 600; }}
    .payload-val {{ flex: 1; color: var(--text-main); font-family: 'JetBrains Mono', monospace; word-break: break-all; }}

    .empty-hero {{
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      color: var(--text-muted);
      text-align: center;
      padding: 40px;
    }}
    .empty-hero svg {{ width: 64px; height: 64px; margin-bottom: 20px; color: #cbd5e1; }}

    /* Scrollbar */
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: #94a3b8; }}
  </style>
</head>
<body>
  <header>
    <div class="logo">
      <div class="logo-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
      </div>
      <span id="header-project-name">NexusFlow Console Pro</span>
    </div>
    <div class="header-actions">
      <div class="daemon-badge">
        <div id="daemon-dot" class="status-dot"></div>
        <span id="daemon-summary">...</span>
      </div>
      <button onclick="toggleLang()" style="background: rgba(255,255,255,0.1); color: white; border: none;">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>
        <span id="lang-text">EN / 中</span>
      </button>
      <button class="primary" onclick="refreshAll()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
        <span id="btn-refresh-text">刷新</span>
      </button>
    </div>
  </header>

  <main>
    <div class="sidebar">
      <div class="sidebar-title" id="sidebar-title">项目分组</div>
      <div class="project-list" id="project-list"></div>
    </div>
    
    <div class="queue-pane">
      <div class="pane-header">
        <div class="pane-title"><span id="queue-pane-title">任务队列</span> <span id="queue-count" style="font-weight:400; color:var(--text-muted); font-size:14px;"></span></div>
      </div>
      <div class="toolbar">
        <button id="btn-start" onclick="postAction('/daemon/start')">启动消费</button>
        <button id="btn-stop" onclick="postAction('/daemon/stop')">暂停消费</button>
        <button id="btn-dispatch-next" onclick="postAction('/daemon/dispatch-next')">派发下一个</button>
        <button id="btn-dispatch-selected" disabled onclick="dispatchSelected()">派发选中</button>
      </div>
      <div class="task-list" id="task-list"></div>
    </div>
    
    <div class="detail-pane" id="detail-pane">
      <div class="empty-hero" id="empty-hero">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
        <div id="no-task-selected-title" style="font-size:18px; font-weight:700; color:var(--text-main);">未选择任务</div>
        <div id="no-task-selected-desc" style="margin-top:8px;">请从左侧列表选择一个任务查看运行详情。</div>
      </div>
    </div>
  </main>

  <script>
    const TRANSLATIONS = {{
      'zh': {{
        'refresh': '刷新',
        'projects': '项目分组',
        'queue': '任务队列',
        'btn_start': '启动消费',
        'btn_stop': '暂停消费',
        'btn_dispatch_next': '派发下一个',
        'btn_dispatch_selected': '派发选中',
        'no_project': '无项目',
        'no_task_in_project': '该项目暂无任务',
        'no_task_selected_title': '未选择任务',
        'no_task_selected_desc': '请从左侧列表选择一个任务查看运行详情。',
        'loading_detail': '加载详情中...',
        'task_detail': '任务详情',
        'task_id': '任务 ID',
        'exit_status': '退出状态',
        'duration': '耗时',
        'updated_at': '更新于',
        'tab_overview': '概览',
        'tab_events': '事件流',
        'tab_prompt': '提示词',
        'tab_console': '控制台输出',
        'tab_patch': '结果补丁',
        'tab_json': '原始 JSON',
        'created_at': '创建时间',
        'finished_at': '结束时间',
        'workspace': '工作目录',
        'command': '执行指令',
        'no_events': '暂无事件记录',
        'no_output': '暂无输出',
        'no_result': '暂无结果',
        'running': '运行中',
        'daemon_active': '自动派发中',
        'daemon_inactive': '派发已停止',
        'pending_jobs': '待处理',
        'default_project': '默认项目',
        'loading': '加载中...',
        'status_pending': '等待派发',
        'status_queued': '队列中',
        'status_dispatching': '派发中',
        'status_dispatched': '已启动',
        'status_running': '执行中',
        'status_succeeded': '已成功',
        'status_completed': '已完成',
        'status_failed': '已失败',
        'status_cancelling': '取消中',
        'status_cancelled': '已取消',
        'evt_job_created': '任务已创建',
        'evt_daemon_dispatch_started': '后台派发启动',
        'evt_daemon_dispatch_retry': '后台重试派发',
        'evt_daemon_worker_spawned': '工作进程已启动',
        'evt_worker_started': 'Worker 开始执行',
        'evt_gemini_prompt_ready': '提示词已就绪',
        'evt_gemini_attempt_started': 'Gemini 尝试开始',
        'evt_gemini_attempt_finished': 'Gemini 尝试结束',
        'evt_patch_capture_skipped': '补丁捕获跳过',
        'evt_worker_finished': 'Worker 执行结束',
        'evt_unknown': '未知事件'
      }},
      'en': {{
        'refresh': 'Refresh',
        'projects': 'Projects',
        'queue': 'Task Queue',
        'btn_start': 'Start Daemon',
        'btn_stop': 'Stop Daemon',
        'btn_dispatch_next': 'Dispatch Next',
        'btn_dispatch_selected': 'Dispatch Selected',
        'no_project': 'No Projects',
        'no_task_in_project': 'No tasks in this project',
        'no_task_selected_title': 'No Task Selected',
        'no_task_selected_desc': 'Select a task from the list to view details.',
        'loading_detail': 'Loading details...',
        'task_detail': 'Task Details',
        'task_id': 'Task ID',
        'exit_status': 'Exit Code',
        'duration': 'Duration',
        'updated_at': 'Updated At',
        'tab_overview': 'Overview',
        'tab_events': 'Events',
        'tab_prompt': 'Prompt',
        'tab_console': 'Console',
        'tab_patch': 'Patch',
        'tab_json': 'Raw JSON',
        'created_at': 'Created At',
        'finished_at': 'Finished At',
        'workspace': 'Workspace',
        'command': 'Command',
        'no_events': 'No events recorded',
        'no_output': 'No console output',
        'no_result': 'No result data',
        'running': 'Running',
        'daemon_active': 'Daemon Active',
        'daemon_inactive': 'Daemon Stopped',
        'pending_jobs': 'Pending',
        'default_project': 'Default Project',
        'loading': 'Loading...',
        'status_pending': 'Pending',
        'status_queued': 'Queued',
        'status_dispatching': 'Dispatching',
        'status_dispatched': 'Dispatched',
        'status_running': 'Running',
        'status_succeeded': 'Succeeded',
        'status_completed': 'Completed',
        'status_failed': 'Failed',
        'status_cancelling': 'Cancelling',
        'status_cancelled': 'Cancelled',
        'evt_job_created': 'Job Created',
        'evt_daemon_dispatch_started': 'Daemon Dispatch Started',
        'evt_daemon_dispatch_retry': 'Daemon Dispatch Retry',
        'evt_daemon_worker_spawned': 'Worker Process Spawned',
        'evt_worker_started': 'Worker Started',
        'evt_gemini_prompt_ready': 'Prompt Ready',
        'evt_gemini_attempt_started': 'Gemini Attempt Started',
        'evt_gemini_attempt_finished': 'Gemini Attempt Finished',
        'evt_patch_capture_skipped': 'Patch Capture Skipped',
        'evt_worker_finished': 'Worker Finished',
        'evt_unknown': 'Unknown Event'
      }}
    }};

    let state = {{
      lang: localStorage.getItem('lang') || (navigator.language.startsWith('zh') ? 'zh' : 'en'),
      projects: {{}},
      jobs: [],
      selectedProject: null,
      selectedJobId: null,
      jobDetail: null,
      activeTab: 'Overview',
      daemon: {{}},
      logOffset: 0,
      logContent: ""
    }};

    function t(key) {{
      return (TRANSLATIONS[state.lang] || TRANSLATIONS['en'])[key] || key;
    }}

    function toggleLang() {{
      state.lang = state.lang === 'zh' ? 'en' : 'zh';
      localStorage.setItem('lang', state.lang);
      // Update tab names if they are currently set to translated versions
      const currentKey = Object.entries(TRANSLATIONS['zh']).find(([k,v]) => v === state.activeTab)?.[0] || 
                         Object.entries(TRANSLATIONS['en']).find(([k,v]) => v === state.activeTab)?.[0];
      if (currentKey) {{
          state.activeTab = t(currentKey);
      }} else {{
          state.activeTab = t('tab_overview');
      }}
      render();
    }}

    const STATUS_MAP = {{
      "pending": {{ label_key: "status_pending", class: "gray", color: "#94a3b8" }},
      "queued": {{ label_key: "status_queued", class: "gray", color: "#94a3b8" }},
      "dispatching": {{ label_key: "status_dispatching", class: "orange", color: "#eab308" }},
      "dispatched": {{ label_key: "status_dispatched", class: "orange", color: "#eab308" }},
      "running": {{ label_key: "status_running", class: "orange", color: "#eab308" }},
      "succeeded": {{ label_key: "status_succeeded", class: "green", color: "#10b981" }},
      "completed": {{ label_key: "status_completed", class: "green", color: "#10b981" }},
      "failed": {{ label_key: "status_failed", class: "red", color: "#ef4444" }},
      "cancelling": {{ label_key: "status_cancelling", class: "orange", color: "#f59e0b" }},
      "cancelled": {{ label_key: "status_cancelled", class: "gray", color: "#94a3b8" }}
    }};

    function formatDate(isoStr, onlyTime = false) {{
      if (!isoStr || isoStr === '-') return '-';
      try {{
        const date = new Date(isoStr);
        if (isNaN(date.getTime())) return isoStr;
        const pad = (n) => String(n).padStart(2, '0');
        const Y = date.getFullYear();
        const M = pad(date.getMonth() + 1);
        const D = pad(date.getDate());
        const h = pad(date.getHours());
        const m = pad(date.getMinutes());
        const s = pad(date.getSeconds());
        return onlyTime ? `${{h}}:${{m}}:${{s}}` : `${{Y}}-${{M}}-${{D}} ${{h}}:${{m}}:${{s}}`;
      }} catch (e) {{ return isoStr; }}
    }}

    function normalize(job) {{
      const status = job.workflow_status || job.dispatch_status || job.status || 'pending';
      return {{
        jobId: job.job_id || '-',
        projectId: job.projectId || job.project_id || job.project || t('default_project'),
        taskId: job.taskId || job.task_id || '-',
        status: status,
        updatedAt: job.updated_at || job.created_at || '-',
      }};
    }}

    async function refreshAll() {{
      try {{
        await Promise.all([refreshRegistry(), refreshDaemon(), refreshJobs()]);
        if (state.selectedJobId && state.activeTab === t("tab_console")) {{
            await fetchIncrementalLogs();
        }}
        render();
      }} catch (e) {{ console.error('Refresh failed', e); }}
    }}

    async function refreshRegistry() {{
      const res = await fetch('/registry');
      const data = await res.json();
      state.projects = data.registry?.projects || {{}};
    }}

    async function refreshDaemon() {{
      const res = await fetch('/daemon/status');
      const data = await res.json();
      state.daemon = data.daemon || {{}};
    }}

    async function refreshJobs() {{
      const res = await fetch('/platform-jobs');
      const data = await res.json();
      state.jobs = (data.jobs || []).map(normalize);
      
      const uniqueProjects = [...new Set(state.jobs.map(j => j.projectId))];
      if (!state.selectedProject && uniqueProjects.length > 0) {{
        state.selectedProject = uniqueProjects[0];
      }}
    }}

    async function selectJob(jobId) {{
      state.selectedJobId = jobId;
      state.jobDetail = null;
      state.logOffset = 0;
      state.logContent = "";
      render();
      
      const res = await fetch(`/platform-jobs/${{jobId}}`);
      if (res.ok) {{
        state.jobDetail = await res.json();
        render();
        // Trigger initial log fetch if Console tab is active
        if (state.activeTab === t('tab_console')) {{
            await fetchIncrementalLogs();
        }}
      }}
    }}

    async function fetchIncrementalLogs() {{
      if (!state.selectedJobId || state.activeTab !== t('tab_console')) return;
      try {{
        const res = await fetch(`/platform-jobs/${{state.selectedJobId}}/logs?offset=${{state.logOffset}}`);
        const data = await res.json();
        if (data.ok && (data.content || data.offset > state.logOffset)) {{
          state.logContent += data.content || "";
          state.logOffset = data.offset;
          const codeBlock = document.getElementById("log-code-block");
          if (codeBlock) {{
            codeBlock.textContent = state.logContent;
            codeBlock.scrollTop = codeBlock.scrollHeight;
          }}
        }}
      }} catch (e) {{ console.error("Log fetch failed", e); }}
    }}

    async function postAction(path) {{
      const res = await fetch(path, {{ method: 'POST' }});
      if (!res.ok) {{
        const data = await res.json();
        alert(state.lang === 'zh' ? `操作失败: ${{data.error || res.statusText}}` : `Action failed: ${{data.error || res.statusText}}`);
      }}
      await refreshAll();
    }}

    async function dispatchSelected() {{
      if (state.selectedJobId) {{
        await postAction(`/daemon/dispatch/${{state.selectedJobId}}`);
      }}
    }}

    function render() {{
      const projName = state.selectedProject || "NexusFlow";
      document.title = state.lang === "zh" ? `${{projName}} 控制台` : `${{projName}} Console`;
      const headerTitle = document.getElementById("header-project-name");
      if (headerTitle) headerTitle.textContent = `${{projName}} Console Pro`;

      document.getElementById('btn-refresh-text').textContent = t('refresh');
      document.getElementById('sidebar-title').textContent = t('projects');
      document.getElementById('queue-pane-title').textContent = t('queue');
      document.getElementById('btn-start').textContent = t('btn_start');
      document.getElementById('btn-stop').textContent = t('btn_stop');
      document.getElementById('btn-dispatch-next').textContent = t('btn_dispatch_next');
      document.getElementById('btn-dispatch-selected').textContent = t('btn_dispatch_selected');
      
      const noTaskTitle = document.getElementById('no-task-selected-title');
      if (noTaskTitle) noTaskTitle.textContent = t('no_task_selected_title');
      const noTaskDesc = document.getElementById('no-task-selected-desc');
      if (noTaskDesc) noTaskDesc.textContent = t('no_task_selected_desc');

      renderDaemon();
      renderProjects();
      renderTasks();
      renderDetail();
    }}

    function renderDaemon() {{
      const d = state.daemon;
      const dot = document.getElementById('daemon-dot');
      dot.className = 'status-dot ' + (d.enabled ? 'active' : 'inactive');
      document.getElementById('daemon-summary').textContent = 
        `${{d.enabled ? t('daemon_active') : t('daemon_inactive')}} · ${{t('pending_jobs')}} ${{d.pending_jobs || 0}}`;
      document.getElementById('btn-start').disabled = d.enabled;
      document.getElementById('btn-stop').disabled = !d.enabled;
    }}

    function renderProjects() {{
      const counts = {{}};
      state.jobs.forEach(j => {{ counts[j.projectId] = (counts[j.projectId] || 0) + 1; }});
      const container = document.getElementById('project-list');
      const projects = Object.keys(counts).sort();
      
      if (projects.length === 0) {{
        container.innerHTML = `<div style="padding:20px; text-align:center; color:var(--text-muted); font-size:13px;">${{t('no_project')}}</div>`;
        return;
      }}

      container.innerHTML = projects.map(p => `
        <div class="project-item ${{state.selectedProject === p ? 'active' : ''}}" onclick="state.selectedProject='${{p}}'; render();">
          <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${{p}}</span>
          <span class="project-badge">${{counts[p]}}</span>
        </div>
      `).join('');
    }}

    function renderTasks() {{
      const tasks = state.jobs.filter(j => j.projectId === state.selectedProject);
      const container = document.getElementById('task-list');
      document.getElementById('queue-count').textContent = tasks.length;

      if (tasks.length === 0) {{
        container.innerHTML = `<div class="empty-hero" style="padding-top:100px;">${{t('no_task_in_project')}}</div>`;
        return;
      }}

      container.innerHTML = tasks.map(t_obj => {{
        const s = STATUS_MAP[t_obj.status] || STATUS_MAP.pending;
        return `
          <div class="task-card ${{state.selectedJobId === t_obj.jobId ? 'active' : ''}}" onclick="selectJob('${{t_obj.jobId}}')">
            <div class="task-card-header">
              <div class="task-name">${{t_obj.jobId}}</div>
              <span class="pill" style="background:${{s.color}}; color:white;">${{t(s.label_key)}}</span>
            </div>
            <div class="task-time">${{formatDate(t_obj.updatedAt, true)}} · ${{t_obj.taskId}}</div>
          </div>
        `;
      }}).join('');
      
      document.getElementById('btn-dispatch-selected').disabled = !state.selectedJobId;
    }}

    function renderDetail() {{
      const container = document.getElementById('detail-pane');
      if (!state.selectedJobId) return; // Empty hero already there
      
      if (!state.jobDetail) {{
        container.innerHTML = `<div class="empty-hero"><div style="animation:pulse 1.5s infinite;">${{t('loading_detail')}}</div></div>`;
        return;
      }}

      const d = state.jobDetail;
      const job = d.job || {{}};
      const status = d.status || {{}};
      const s = STATUS_MAP[status.status || job.status || 'pending'] || STATUS_MAP.pending;

      const tabs = [
          {{ key: 'tab_overview', label: t('tab_overview') }},
          {{ key: 'tab_events', label: t('tab_events') }},
          {{ key: 'tab_prompt', label: t('tab_prompt') }},
          {{ key: 'tab_console', label: t('tab_console') }},
          {{ key: 'tab_patch', label: t('tab_patch') }},
          {{ key: 'tab_json', label: t('tab_json') }}
      ];

      container.innerHTML = `
        <div class="detail-header">
          <div class="detail-title-row">
            <div>
              <div style="font-size:12px; color:var(--text-muted); font-weight:600; text-transform:uppercase;">${{t('task_detail')}}</div>
              <div style="font-size:20px; font-weight:800; margin-top:4px;">${{job.job_id}}</div>
            </div>
            <span class="pill" style="font-size:12px; padding:4px 12px; background:${{s.color}}; color:white;">${{t(s.label_key)}}</span>
          </div>
          <div class="stats-grid">
            <div class="stat-item"><div class="stat-label">${{t('task_id')}}</div><div class="stat-value">${{status.task_id || job.taskId || '-'}}</div></div>
            <div class="stat-item"><div class="stat-label">${{t('exit_status')}}</div><div class="stat-value">${{status.returncode ?? t('running')}}</div></div>
            <div class="stat-item"><div class="stat-label">${{t('duration')}}</div><div class="stat-value">${{calculateDuration(status)}}</div></div>
            <div class="stat-item"><div class="stat-label">${{t('updated_at')}}</div><div class="stat-value">${{formatDate(status.updated_at, true)}}</div></div>
          </div>
        </div>
        <div class="tabs">
          ${{tabs.map(tab => `
            <div class="tab ${{state.activeTab === tab.label ? 'active' : ''}}" onclick="state.activeTab='${{tab.label}}'; renderDetail();">${{tab.label}}</div>
          `).join('')}}
        </div>
        <div class="tab-body">
          ${{renderTabContent(state.activeTab, d)}}
        </div>
      `;
    }}

    function renderTabContent(tabLabel, data) {{
      const tabKey = Object.entries(TRANSLATIONS[state.lang]).find(([k,v]) => v === tabLabel)?.[0] || 
                     Object.entries(TRANSLATIONS['en']).find(([k,v]) => v === tabLabel)?.[0] ||
                     Object.entries(TRANSLATIONS['zh']).find(([k,v]) => v === tabLabel)?.[0];
      
      switch(tabKey) {{
        case 'tab_overview':
          return `
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px;">
              <div class="stat-item"><div class="stat-label">${{t('created_at')}}</div><div class="stat-value">${{formatDate(data.status?.created_at || data.job?.created_at)}}</div></div>
              <div class="stat-item"><div class="stat-label">${{t('finished_at')}}</div><div class="stat-value">${{formatDate(data.status?.finished_at)}}</div></div>
              <div class="stat-item" style="grid-column: span 2;"><div class="stat-label">${{t('workspace')}}</div><div class="stat-value" style="word-break:break-all;">${{data.job?.workspace || '-'}}</div></div>
              <div class="stat-item" style="grid-column: span 2;"><div class="stat-label">${{t('command')}}</div><div class="stat-value" style="word-break:break-all; font-family:monospace;">${{data.status?.worker_command?.join(' ') || '-'}}</div></div>
            </div>
          `;
        case 'tab_events':
          return renderTimeline(data.events_tail);
        case 'tab_prompt':
          return `<pre class="code-block">${{escapeHtml(data.status?.prompt || data.job?.prompt || '-')}}</pre>`;
        case 'tab_console':
          return `<pre id="log-code-block" class="code-block" style="height:500px; overflow-y:auto;">${{escapeHtml(state.logContent || t('no_output'))}}</pre>`;
        case 'tab_patch':
          return `<pre class="code-block">${{escapeHtml(data.result_tail || t('no_result'))}}</pre>`;
        case 'tab_json':
          return `<pre class="code-block">${{escapeHtml(JSON.stringify(data, null, 2))}}</pre>`;
        default: return '';
      }}
    }}

    function renderTimeline(eventsText) {{
      if (!eventsText) return `<div class="empty-hero">${{t("no_events")}}</div>`;
      const lines = eventsText.trim().split('\\n');
      
      const icons = {{
        success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>',
        error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M18 6L6 18M6 6l12 12"/></svg>',
        info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
        process: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/></svg>',
        default: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/></svg>'
      }};

      return `<div class="timeline">` + lines.map(line => {{
        try {{
          const e = JSON.parse(line);
          const rawEvt = e.event || "unknown";
          const displayTitle = t("evt_" + rawEvt) || rawEvt;
          
          let type = "default";
          let icon = icons.default;
          
          if (rawEvt.includes("succeeded") || rawEvt.includes("completed") || rawEvt.includes("finished")) {{ 
            if (!rawEvt.includes("failed")) {{ type = "success"; icon = icons.success; }}
            else {{ type = "error"; icon = icons.error; }}
          }}
          else if (rawEvt.includes("failed") || rawEvt.includes("error")) {{ type = "error"; icon = icons.error; }}
          else if (rawEvt.includes("spawned") || rawEvt.includes("started") || rawEvt.includes("dispatch") || rawEvt.includes("ready")) {{ 
            type = "processing"; icon = icons.process; 
          }}

          let payloadHtml = "";
          if (e.payload && Object.keys(e.payload).length > 0) {{
            const rows = Object.entries(e.payload).map(([k, v]) => {{
              const valStr = typeof v === "object" ? JSON.stringify(v) : String(v);
              return `<div class="payload-row"><div class="payload-key">${{escapeHtml(k)}}</div><div class="payload-val">${{escapeHtml(valStr)}}</div></div>`;
            }}).join("");
            payloadHtml = `<div class="timeline-payload">${{rows}}</div>`;
          }

          return `
            <div class="timeline-item ${{type}}">
              <div class="timeline-marker">${{icon}}</div>
              <div class="timeline-content">
                <div class="timeline-header">
                  <div class="timeline-title">${{escapeHtml(displayTitle)}}</div>
                  <span class="timeline-time">${{formatDate(e.time)}}</span>
                </div>
                ${{payloadHtml}}
              </div>
            </div>
          `;
        }} catch(err) {{ 
           return `<div class="timeline-item"><div class="timeline-marker">${{icons.default}}</div><div class="timeline-content" style="font-family:monospace;font-size:12px;">${{escapeHtml(line)}}</div></div>`; 
        }}
      }}).join("") + `</div>`;
    }}

    function calculateDuration(status) {{
      if (!status.started_at || !status.finished_at) return '-';
      const diff = Math.floor((new Date(status.finished_at) - new Date(status.started_at)) / 1000);
      return diff < 0 ? '-' : diff + 's';
    }}

    function escapeHtml(value) {{
      if (!value) return '';
      return String(value).replace(/[&<>"']/g, char => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}}[char]));
    }}

    refreshAll();
    setInterval(refreshAll, 5000);
  </script>
</body>
</html>"""


class BridgeError(Exception):
    def __init__(self, message: str, status_code: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiBridgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/dashboard")
            self.end_headers()
            return
        if parsed.path == "/dashboard":
            self.write_html(HTTPStatus.OK, dashboard_html())
            return
        if parsed.path == "/health":
            self.write_json(HTTPStatus.OK, {"ok": True, "status": "healthy", "utc_now": utc_now()})
            return
        if parsed.path == "/registry":
            self.write_json(HTTPStatus.OK, {"ok": True, "registry": read_registry()})
            return
        if parsed.path == "/daemon/status":
            self.write_json(HTTPStatus.OK, {"ok": True, "daemon": DAEMON.status()})
            return
        if parsed.path == "/platform-projects":
            registry = read_registry()
            self.write_json(HTTPStatus.OK, {"ok": True, "projects": registry.get("projects", {})})
            return
        if parsed.path == "/platform-agents":
            registry = read_registry()
            self.write_json(HTTPStatus.OK, {"ok": True, "agents": registry.get("agents", {})})
            return
        if parsed.path == "/platform-jobs":
            self.write_json(HTTPStatus.OK, {"ok": True, "jobs": list_platform_jobs()})
            return
        if parsed.path.startswith("/platform-jobs/") and parsed.path.endswith("/logs"):
            job_id = parsed.path.removeprefix("/platform-jobs/").removesuffix("/logs").strip("/")
            query = parse_qs(parsed.query)
            offset = int(query.get("offset", ["0"])[0])
            detail = platform_job_detail(job_id)
            if not detail:
                self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Job not found"})
                return
            
            stdout_path = Path(str(detail["job"]["workspace"])) / detail["job"]["workflow_dir"] / "jobs" / job_id / "stdout.log"
            if not stdout_path.exists():
                self.write_json(HTTPStatus.OK, {"ok": True, "content": "", "offset": 0})
                return
            
            with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                file_size = f.tell()
                if offset > file_size:
                    offset = file_size
                f.seek(offset)
                content = f.read(10000) # Read up to 10KB
                new_offset = f.tell()
                
            self.write_json(HTTPStatus.OK, {"ok": True, "content": content, "offset": new_offset, "total_size": file_size})
            return

        if parsed.path.startswith("/platform-jobs/"):
            job_id = parsed.path.removeprefix("/platform-jobs/").strip("/")
            detail = platform_job_detail(job_id)
            if detail is None:
                self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Platform job not found"})
                return
            self.write_json(HTTPStatus.OK, detail)
            return
        self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/daemon/start":
            DAEMON.set_enabled(True)
            self.write_json(HTTPStatus.OK, {"ok": True, "daemon": DAEMON.status()})
            return
        if parsed.path == "/daemon/stop":
            DAEMON.set_enabled(False)
            self.write_json(HTTPStatus.OK, {"ok": True, "daemon": DAEMON.status()})
            return
        if parsed.path == "/daemon/dispatch-next":
            try:
                dispatched = DAEMON.dispatch_pending_jobs(limit=1)
                self.write_json(HTTPStatus.OK, {"ok": True, "dispatched": dispatched, "daemon": DAEMON.status()})
            except BridgeError as exc:
                self.write_json(exc.status_code, {"ok": False, "error": str(exc), "daemon": DAEMON.status()})
            except Exception as exc:
                self.write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc), "daemon": DAEMON.status()})
            return
        if parsed.path.startswith("/daemon/dispatch/"):
            job_id = parsed.path.removeprefix("/daemon/dispatch/").strip("/")
            try:
                dispatched = DAEMON.dispatch_job_by_id(job_id)
                self.write_json(HTTPStatus.OK, {"ok": True, "dispatched": dispatched, "daemon": DAEMON.status()})
            except BridgeError as exc:
                self.write_json(exc.status_code, {"ok": False, "error": str(exc), "daemon": DAEMON.status()})
            except Exception as exc:
                self.write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc), "daemon": DAEMON.status()})
            return
        if parsed.path == "/gemini":
            self.write_json(
                HTTPStatus.GONE,
                {
                    "ok": False,
                    "error": "Direct Gemini execution was removed. Use the async worker flow from MCP.",
                },
            )
            return
        self.write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def handle_gemini(self) -> None:
        try:
            payload = build_payload(self)
            prompt, timeout, cwd = normalize_request(payload)
            result = try_gemini(prompt=prompt, timeout=timeout, cwd=cwd)
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
            self.write_json(status, result)
        except BridgeError as exc:
            self.write_json(exc.status_code, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover
            self.write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"Unexpected error: {exc}"},
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def has_model_arg(args: list[str]) -> bool:
    return any(
        arg in {"--model", "-m"} or arg.startswith("--model=") or arg.startswith("-m=")
        for arg in args
    )


def default_gemini_model() -> str:
    return os.getenv("GEMINI_DEFAULT_MODEL", DEFAULT_GEMINI_MODEL)


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


def read_available_fd(fd: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    eof = False
    while True:
        try:
            data = os.read(fd, 4096)
            if not data:
                eof = True
                break
            chunks.append(data)
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            eof = True
            break
    return b"".join(chunks), eof


def run_attempt_with_output_timeout(
    command: list[str],
    stdin_text: str | None,
    timeout: int,
    cwd: Path | None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["PATH"] = default_child_path()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
        close_fds=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    streams: dict[int, str] = {}

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

    status = "completed"
    last_output_at = time.time()
    try:
        while True:
            if not streams and proc.poll() is not None:
                break
            remaining = timeout - (time.time() - last_output_at)
            if remaining <= 0:
                status = f"no output timeout after {timeout} seconds"
                proc.terminate()
                break
            ready, _, _ = select.select(list(streams), [], [], min(0.25, remaining))
            if ready:
                for fd in ready:
                    data, eof = read_available_fd(fd)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        if streams.get(fd) == "stdout":
                            stdout_chunks.append(text)
                        else:
                            stderr_chunks.append(text)
                        last_output_at = time.time()
                    if eof:
                        streams.pop(fd, None)
            elif proc.poll() is not None:
                for fd in list(streams):
                    data, eof = read_available_fd(fd)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        if streams.get(fd) == "stdout":
                            stdout_chunks.append(text)
                        else:
                            stderr_chunks.append(text)
                    if eof:
                        streams.pop(fd, None)
                if not streams:
                    break
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    return {
        "ok": proc.returncode == 0,
        "status": status,
        "returncode": proc.returncode,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
    }


def try_gemini(prompt: str, timeout: int, cwd: Path | None) -> dict[str, Any]:
    base_args = with_default_approval_mode(with_default_model(list(CONFIG.base_args)))
    attempts = [
        {
            "strategy": "flag_-p",
            "command": [CONFIG.gemini_bin, *base_args, "-p", prompt],
            "stdin": None,
        },
        {
            "strategy": "flag_--prompt",
            "command": [CONFIG.gemini_bin, *base_args, "--prompt", prompt],
            "stdin": None,
        },
        {
            "strategy": "stdin",
            "command": [CONFIG.gemini_bin, *base_args],
            "stdin": prompt,
        },
    ]

    failures: list[dict[str, Any]] = []
    for attempt in attempts:
        try:
            completed = run_attempt_with_output_timeout(
                command=list(attempt["command"]),
                stdin_text=attempt["stdin"],
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError:
            return {"ok": False, "error": f"Gemini binary not found: {CONFIG.gemini_bin}"}

        if completed["ok"]:
            return {
                "ok": True,
                "strategy": attempt["strategy"],
                "command": attempt["command"],
                "returncode": completed["returncode"],
                "stdout": completed["stdout"],
                "stderr": completed["stderr"],
            }
        failures.append(
            {
                "strategy": attempt["strategy"],
                "command": attempt["command"],
                "returncode": completed["returncode"],
                "stdout": completed["stdout"],
                "stderr": completed["stderr"],
            }
        )

    return {"ok": False, "error": "All attempts failed", "failures": failures}


def build_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("content-length", 0))
    if not content_length:
        return {}
    return json.loads(handler.rfile.read(content_length).decode("utf-8"))


def normalize_request(payload: dict[str, Any]) -> tuple[str, int, Path | None]:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise BridgeError("Prompt is required")
    timeout = env_int("GEMINI_TIMEOUT_SECONDS", 360)
    if payload.get("timeout"):
        try:
            timeout = int(payload["timeout"])
        except ValueError:
            pass
    cwd = None
    if payload.get("cwd"):
        cwd = Path(str(payload["cwd"])).expanduser().resolve()
    return prompt, timeout, cwd


def main() -> None:
    parser = argparse.ArgumentParser(description="NexusFlow Bridge Server - Local HTTP bridge for Gemini CLI.")
    parser.add_argument("--host", default=CONFIG.host, help=f"Host to bind (default: {CONFIG.host})")
    parser.add_argument("--port", type=int, default=CONFIG.port, help=f"Port to bind (default: {CONFIG.port})")
    parser.add_argument("--bin", default=CONFIG.gemini_bin, help=f"Path to gemini binary (default: {CONFIG.gemini_bin})")
    parser.add_argument("--poll", type=float, default=CONFIG.daemon_poll_seconds, help=f"Daemon poll interval in seconds (default: {CONFIG.daemon_poll_seconds})")
    parser.add_argument("--max-jobs", type=int, default=CONFIG.daemon_max_jobs_per_tick, help=f"Max concurrent jobs per tick (default: {CONFIG.daemon_max_jobs_per_tick})")
    parser.add_argument("--no-daemon", action="store_true", help="Disable the automatic job consumer daemon")
    
    args = parser.parse_args()
    
    # Update global config from CLI args
    CONFIG.host = args.host
    CONFIG.port = args.port
    CONFIG.gemini_bin = args.bin
    CONFIG.daemon_poll_seconds = args.poll
    CONFIG.daemon_max_jobs_per_tick = args.max_jobs
    if args.no_daemon:
        CONFIG.daemon_enabled = False
        DAEMON.set_enabled(False)

    DAEMON.start_thread()
    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), GeminiBridgeHandler)
    print(
        f"NexusFlow 服务已启动: http://{CONFIG.host}:{CONFIG.port}/dashboard",
        flush=True,
    )
    print(
        f"后台自动消费: {'开启' if DAEMON.enabled else '关闭'}; 间隔={CONFIG.daemon_poll_seconds}s; 并发={CONFIG.daemon_max_jobs_per_tick}; worker={CONFIG.worker_script}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
