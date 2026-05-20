# NexusFlow: Codex to AI Agent Bridge

English | [中文](#中文说明)

**NexusFlow** is a unified MCP bridge and asynchronous dispatcher for AI Agents. It allows you to connect IDE extensions (like Codex) or other LLM clients to local CLI-based AI workflows with high reliability, parallel execution, and deep visibility.

Primary call chain:

`Codex App` -> `MCP Client` -> `NexusFlow MCP Server` -> `async worker` -> `AI Agent (e.g. Gemini CLI)` -> `docs/ai-workflow/jobs/<job_id>/`

---

## 🚀 Key Features

- **Asynchronous Execution**: Offload heavy AI tasks to background workers. Close your IDE or switch branches; the task keeps running.
- **Pro Console (I18n)**: A built-in dashboard (English/Chinese) to monitor jobs, view live event streams, and debug output.
- **High Resilience**: Automatic retry mechanism (up to 3 times) with configurable delays to handle transient network issues.
- **Parallel Dispatching**: Configurable concurrency (`--max-jobs`) to process multiple development tasks simultaneously.
- **AI-Agnostic Design**: Built to bridge any CLI-based AI agent, providing a standardized MCP interface.

---

## 🛠 Usage Examples

### 1. The "Code Audit" Workflow
Ask Codex to perform a deep audit on your project.
*   **Action**: Codex writes a detailed task file to `docs/ai-workflow/tasks/todo/audit.md`.
*   **NexusFlow**: Automatically picks up the task, spawns a worker, and runs your AI Agent to scan the codebase.
*   **Result**: You can watch the "Event Stream" in the Pro Console and review the generated `patch.patch` when finished.

### 2. Parallel Module Implementation
Working on a frontend and backend simultaneously?
*   **Action**: Dispatch two tasks: `impl-api-auth` and `impl-login-ui`.
*   **NexusFlow**: If `--max-jobs` is set to 2, both workers start immediately in parallel.
*   **Efficiency**: Halves the wait time for complex cross-stack features.

### 3. Hands-free Reliability
Running tasks overnight or on a shaky connection?
*   **Scenario**: AI Agent hits a network timeout 5 minutes into a 10-minute task.
*   **NexusFlow**: Detects the failure, waits 10 seconds, and **automatically retries**. It keeps trying up to 3 times before alerting you.

---

## ⚡ Quick Start

1.  **Environment**: Ensure your AI Agent CLI (e.g., `gemini`) is installed and authenticated.
2.  **Launch Bridge**:
    ```bash
    python3 bridge/gemini_bridge_server.py --poll 1.0 --max-jobs 2
    ```
3.  **Register Project**: Use your IDE (via MCP) or the dashboard to register your project workspace.
4.  **Run**: Create task files in `docs/ai-workflow/tasks/todo/` and notify NexusFlow via the `notify_task_ready` MCP tool.

---

## ⚙️ Configuration

NexusFlow supports both CLI arguments and environment variables:

| Argument | Env Var | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | `GEMINI_BRIDGE_HOST` | `127.0.0.1` | Server binding address. |
| `--port` | `GEMINI_BRIDGE_PORT` | `8787` | Dashboard and API port. |
| `--poll` | `GEMINI_BRIDGE_DAEMON_POLL_SECONDS` | `5.0` | Scanning interval for new tasks. |
| `--max-jobs` | `GEMINI_BRIDGE_DAEMON_MAX_JOBS_PER_TICK` | `1` | Concurrent jobs per scan cycle. |
| `--bin` | `GEMINI_BIN` | `gemini` | Path to the AI Agent CLI. |
| (N/A) | `GEMINI_BRIDGE_DAEMON_MAX_RETRIES` | `3` | Maximum automatic retries. |
| (N/A) | `GEMINI_BRIDGE_DAEMON_RETRY_DELAY` | `10.0` | Delay (seconds) before retrying. |

---

# 中文说明

# NexusFlow: Codex to AI Agent 桥接器

**NexusFlow** 是一个统一的 MCP 桥接器和 AI Agent 异步派发引擎。它允许您将 IDE 插件（如 Codex）连接到本地的 AI 命令行工具流，并提供高可靠性、并行执行和全透明的任务监控。

主链路：

`Codex App` -> `MCP Client` -> `NexusFlow MCP Server` -> `异步 Worker` -> `AI Agent (如 Gemini CLI)` -> `docs/ai-workflow/jobs/<job_id>/`

---

## 🚀 核心特性

- **异步非阻塞**：将繁重的 AI 开发任务交给后台 Worker。您可以关闭 IDE 或切换分支，任务依然会持续运行。
- **专业版控制台 (i18n)**：内置中英文双语 Dashboard，实时监控任务状态、事件流日志和输出结果。
- **高可用性**：内置自动重试机制（默认 3 次），并可配置延迟时间，自动化应对网络波动或超时。
- **并行派发**：可自定义并发度 (`--max-jobs`)，支持同时处理多个复杂的开发任务。
- **通用架构**：旨在桥接任何基于 CLI 的 AI Agent（现已原生支持 Gemini CLI），提供标准化的 MCP 接口。

---

## 🛠 典型应用案例

### 1. “深度代码审计”工作流
让 Codex 对您的整个项目进行安全审计。
*   **操作**：Codex 在 `docs/ai-workflow/tasks/todo/audit.md` 写入审计需求。
*   **NexusFlow**：自动识别任务并启动 Worker，驱动 AI 代理扫描代码库。
*   **结果**：您可以在控制台实时查看审计进度，并在完成后直接获取 `patch.patch` 补丁。

### 2. 多模块并行开发
同时进行前后端功能的实现。
*   **操作**：同时派发 `impl-api-auth`（接口鉴权）和 `impl-login-ui`（登录页面）两个任务。
*   **NexusFlow**：如果设置了 `--max-jobs 2`，两个任务会立即同时开始运行。
*   **效率**：大幅缩短了跨栈功能开发的等待时间。

### 3. 无人值守的任务保障
在网络不稳定或需要长时间运行任务（如夜间任务）时。
*   **场景**：AI 代理执行到一半时由于网络波动超时。
*   **NexusFlow**：自动检测到失败，等待 10 秒后**自动重启任务**。在最终放弃前，它会默默为您尝试 3 次。

---

## ⚡ 快速开始

1.  **环境准备**：确保您的 AI Agent CLI（如 `gemini`）已安装并在终端完成授权。
2.  **启动 Bridge**：
    ```bash
    python3 bridge/gemini_bridge_server.py --poll 1.0 --max-jobs 2
    ```
3.  **注册项目**：通过 IDE（调用 MCP 工具）或在控制台中注册您的项目工作区。
4.  **运行工作流**：在 `docs/ai-workflow/tasks/todo/` 下创建任务文件，通过 MCP 调用 `notify_task_ready` 即可开启异步开发。

---

## ⚙️ 详细参数配置

| 命令行参数 | 环境变量 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- |
| `--host` | `GEMINI_BRIDGE_HOST` | `127.0.0.1` | 服务监听地址。 |
| `--port` | `GEMINI_BRIDGE_PORT` | `8787` | 控制台及 API 端口。 |
| `--poll` | `GEMINI_BRIDGE_DAEMON_POLL_SECONDS` | `5.0` | 后台扫描新任务的频率（秒）。 |
| `--max-jobs` | `GEMINI_BRIDGE_DAEMON_MAX_JOBS_PER_TICK` | `1` | 每个扫描周期允许新启动的任务数。 |
| `--bin` | `GEMINI_BIN` | `gemini` | AI Agent 命令行工具的路径。 |
| (不适用) | `GEMINI_BRIDGE_DAEMON_MAX_RETRIES` | `3` | 任务失败后的最大自动重试次数。 |
| (不适用) | `GEMINI_BRIDGE_DAEMON_RETRY_DELAY` | `10.0` | 自动重试前的等待延迟（秒）。 |

---

## 📝 发布说明

- **隐私保护**：本仓库代码已完成脱敏，移除了所有个人路径信息。
- **配置分离**：建议将个人 API Key 和路径配置保留在本地 `.mcp.json` 中。
- **开源协议**：本项目采用 MIT 协议 (Copyright (c) 2026 NortonYang)。
