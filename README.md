# NexusFlow: Codex to AI Agent Bridge

English | [中文](#中文说明)

**NexusFlow** is a unified MCP bridge and asynchronous dispatcher for AI Agents. It allows you to connect IDE extensions (like Codex) or other LLM clients to local CLI-based AI workflows with high reliability and visibility.

Primary call chain:

`Codex App` -> `MCP Client` -> `NexusFlow MCP Server` -> `async worker` -> `AI Agent (e.g. Gemini CLI)` -> `docs/ai-workflow/jobs/<job_id>/`

## Key Features

- **Asynchronous Execution**: Offload heavy AI tasks to background workers, freeing up your IDE.
- **Multilingual Dashboard**: A built-in Pro Console (English/Chinese) to monitor jobs, event logs, and output in real-time.
- **Resilience**: Automatic retry mechanism (up to 3 times) for transient network or timeout failures.
- **Parallel Dispatch**: Configurable concurrency to handle multiple development tasks simultaneously.
- **AI-Agnostic**: Designed to bridge any CLI-based AI agent, starting with Gemini CLI support.

## Structure

- `bridge/gemini_bridge_server.py`
  The core NexusFlow Bridge Server. Manages the registry, the job queue, and the dashboard.
- `plugins/gemini-bridge/.codex-plugin/plugin.json`
  Codex plugin manifest for NexusFlow.
- `plugins/gemini-bridge/scripts/gemini_bridge_mcp.py`
  The stdio MCP server that integrates with your IDE to queue jobs and fetch status.
- `LICENSE`
  MIT License.

## Quick Start

1. **Setup Environment**: Ensure your AI Agent CLI (e.g., `gemini`) is installed and authenticated in your terminal.
2. **Launch Bridge**:
   ```bash
   python3 bridge/gemini_bridge_server.py --poll 1.0 --max-jobs 2
   ```
3. **Register Project**: Use your IDE (via MCP) or the dashboard to register your project workspace.
4. **Run Workflows**: Create task files in `docs/ai-workflow/tasks/todo/` and notify NexusFlow via MCP to start the asynchronous magic.

## Pro Console

Open the dashboard to see NexusFlow in action:

```bash
open http://127.0.0.1:8787/dashboard
```

- **Live Monitoring**: Watch stdout/stderr and event streams as they happen.
- **Manual Override**: Force-restart stuck jobs or manually dispatch queued tasks.
- **I18n**: Click the globe icon to switch between English and Chinese.

## Configuration

NexusFlow can be configured via environment variables or command-line arguments:

| Argument | Env Var | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | `GEMINI_BRIDGE_HOST` | `127.0.0.1` | Host to bind the server. |
| `--port` | `GEMINI_BRIDGE_PORT` | `8787` | Port for the dashboard and API. |
| `--poll` | `GEMINI_BRIDGE_DAEMON_POLL_SECONDS` | `5.0` | Job scanning interval in seconds. |
| `--max-jobs` | `GEMINI_BRIDGE_DAEMON_MAX_JOBS_PER_TICK` | `1` | Max new jobs to start per poll tick. |
| `--bin` | `GEMINI_BIN` | `gemini` | Path to the AI Agent binary. |
| (N/A) | `GEMINI_BRIDGE_DAEMON_MAX_RETRIES` | `3` | Max auto-retry attempts for failed jobs. |
| (N/A) | `GEMINI_BRIDGE_DAEMON_RETRY_DELAY` | `10.0` | Delay in seconds before retrying. |

## Notes

- **Workflow Directory**: Each project must have a `docs/ai-workflow/` directory structure (`tasks/todo`, `tasks/working`, `jobs`) to store its local AI development history.
- **Headless Mode**: Use `--no-daemon` if you only want the API/Dashboard without automatic background consumption.

---

# 中文说明

# NexusFlow: Codex to AI Agent 桥接器

**NexusFlow** 是一个统一的 MCP 桥接器和 AI Agent 异步派发引擎。它允许您将 IDE 插件（如 Codex）连接到本地的 AI 命令行工具流，并提供极高的可靠性和透明度。

主链路：

`Codex App` -> `MCP Client` -> `NexusFlow MCP Server` -> `异步 Worker` -> `AI Agent (如 Gemini CLI)` -> `docs/ai-workflow/jobs/<job_id>/`

## 核心特性

- **异步执行**：将繁重的 AI 任务交给后台 Worker，不阻塞 IDE。
- **多语言控制台**：内置专业版控制台（中英文），实时监控任务状态、事件流和日志。
- **高可用性**：内置自动重试机制（默认 3 次），自动应对网络波动或超时。
- **并行派发**：可配置的并发度，支持同时处理多个开发任务。
- **通用架构**：旨在桥接任何基于 CLI 的 AI Agent（现已原生支持 Gemini CLI）。

## 快速开始

1. **环境准备**：确保您的 AI Agent CLI（如 `gemini`）已安装并在终端完成授权。
2. **启动 Bridge**：
   ```bash
   python3 bridge/gemini_bridge_server.py --poll 1.0 --max-jobs 2
   ```
3. **注册项目**：通过 IDE（调用 MCP 工具）或在控制台中注册您的项目工作区。
4. **运行工作流**：在 `docs/ai-workflow/tasks/todo/` 下创建任务文件，通过 MCP 通知 NexusFlow，即可开启异步开发。

## 控制台 (Dashboard)

打开浏览器访问 NexusFlow 控制台：

```bash
open http://127.0.0.1:8787/dashboard
```

- **实时监控**：实时查看 stdout/stderr 日志和详细的事件时间轴。
- **手动干预**：可以强制重启卡住的任务，或手动派发排队中的任务。
- **多语言**：点击顶部的地球图标即可一键切换中英文。

## 详细参数配置

可以通过命令行参数或环境变量进行配置：

*   `--poll <秒>`: 后台扫描任务的频率（建议 1.0）。
*   `--max-jobs <个数>`: 每个周期最多同时新启动的任务数。
*   `--port <端口>`: 控制台监听端口（默认 8787）。
*   `GEMINI_BRIDGE_DAEMON_MAX_RETRIES`: 任务失败后的最大重试次数（默认 3）。

## 发布说明

- **隐私保护**：本仓库的代码已经过脱敏处理，不包含任何个人绝对路径。
- **配置分离**：请将您的个人 API Key 和路径配置保留在本地的 `.mcp.json` 或环境变量中，不要提交到公共仓库。
- **开源协议**：本项目采用 MIT 协议。
