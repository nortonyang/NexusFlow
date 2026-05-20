# NexusFlow: Codex to Gemini CLI Bridge

English | [中文](#中文说明)

This repository provides a local Codex plugin and MCP bridge that dispatch workflow task files to the real `gemini` CLI through an asynchronous worker.

Primary call chain:

`Codex App` -> `MCP Client` -> `NexusFlow MCP Server` -> `async worker` -> `gemini -p` -> `docs/ai-workflow/jobs/<job_id>/`

## Structure

- `bridge/gemini_bridge_server.py`
  Optional local dashboard server for registry and async job status. It does not execute Gemini directly.
- `plugins/gemini-bridge/.codex-plugin/plugin.json`
  Codex plugin manifest.
- `plugins/gemini-bridge/.mcp.json`
  MCP server wiring for the plugin.
- `plugins/gemini-bridge/scripts/gemini_bridge_mcp.py`
  Local stdio MCP server that exposes registry and async worker tools.
- `mcp/gemini_codex_mcp.py`
  Optional reverse-direction MCP server for Gemini CLI experiments.
- `.agents/plugins/marketplace.json`
  Repo-local marketplace entry so Codex can discover the plugin.

## Quick Start

1. Install or load the local plugin in Codex App from this repository.
2. Make sure the `gemini` CLI already works in your real terminal and can reuse your Google login.
3. Point the plugin MCP config at a Python runtime and the Gemini CLI available on your machine.
4. Register a project, write a task file under `docs/ai-workflow/tasks/todo/`, then notify the local queue. The dashboard daemon starts Gemini workers outside the Codex MCP call path.

```text
Use NexusFlow: register this project and notify task ready.
```

The stdio MCP tool does not require the HTTP bridge server. The HTTP server is only for dashboard and JSON status endpoints.

## Recommended Local MCP Configuration

For a portable setup, keep machine-specific values in your local `.mcp.json` or environment instead of hardcoding them in documentation.

Example:

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "command": "python3",
      "args": [
        "<repo-root>/plugins/gemini-bridge/scripts/gemini_bridge_mcp.py"
      ],
      "cwd": "<repo-root>",
      "env": {
        "GEMINI_BIN": "/path/to/gemini",
        "GEMINI_HOME": "/path/to/home"
      }
    }
  }
}
```

Notes:

- `GEMINI_BIN` should point to the Gemini CLI you actually use in your terminal.
- `GEMINI_HOME` / `GEMINI_CLI_HOME` should point to the home directory that contains `.gemini/`, for example `/Users/you`, not `/Users/you/.gemini`.
- If a `.gemini` directory is provided by mistake, the bridge normalizes it to its parent before launching Gemini CLI.
- Do not set `GEMINI_AUTH_FILE` for normal Gemini CLI user OAuth unless you intentionally want to force `GOOGLE_APPLICATION_CREDENTIALS`. The CLI usually resolves user OAuth from `HOME/.gemini` or its own keychain-backed storage.
- You can also omit these and let the bridge resolve them from the local environment when possible.

## MCP Tools

Only the async worker platform tools are exposed:

- `register_project`
- `list_projects`
- `get_project`
- `unregister_project`
- `register_agent`
- `list_agents`
- `notify_task_ready`
- `list_platform_jobs`
- `get_workflow_job`
- `cancel_workflow_job`

Default model:

- The bridge uses `gemini-3-flash-preview` by default.
- If `gemini_args` or `GEMINI_BASE_ARGS` already include `--model`, that explicit value wins.
- You can also override the default with `GEMINI_DEFAULT_MODEL`.

Direct prompt execution and dispatch tools are intentionally not exposed to Codex. Codex can queue tasks and read status; the local HTTP bridge daemon is responsible for starting Gemini workers.

## HTTP Bridge API

### Health Check

```bash
curl http://127.0.0.1:8787/health
```

### Dashboard

```bash
open http://127.0.0.1:8787/dashboard
```

The dashboard is a built-in Chinese single-page UI served by the HTTP bridge. It has no frontend build step and is deployed together with `bridge/gemini_bridge_server.py`.

The `/gemini` HTTP execution endpoint has been removed. Use MCP async worker tools instead.

### Local Daemon

The dashboard backend can consume queued platform jobs and start Gemini workers locally. This is intentionally outside the Codex MCP call path.

```bash
open http://127.0.0.1:8787/dashboard
```

Use the dashboard controls to start or stop the daemon, or start the server with:

```bash
GEMINI_BRIDGE_DAEMON_ENABLED=1 python3 bridge/gemini_bridge_server.py
```

For a persistent local background server, use:

```bash
GEMINI_BRIDGE_WORKSPACE=/path/to/project scripts/start_gemini_bridge_server.sh
scripts/stop_gemini_bridge_server.sh
```

The start script prints the dashboard URL, health-check URL, and log path. You can override the Python runtime with `PYTHON_BIN=/path/to/python3`.

On macOS, prefer LaunchAgent if the server should survive terminal/session shutdown:

```bash
GEMINI_BRIDGE_WORKSPACE=/path/to/project scripts/install_gemini_bridge_launch_agent.sh
scripts/uninstall_gemini_bridge_launch_agent.sh
```

## Environment Variables

### Bridge Server

- `GEMINI_BRIDGE_HOST`
  Default: `127.0.0.1`
- `GEMINI_BRIDGE_PORT`
  Default: `8787`
- `GEMINI_BRIDGE_DAEMON_ENABLED`
  Default: `1`. Set to `0` only when you want to pause automatic consumption and dispatch queued jobs manually from the dashboard.
- `GEMINI_BRIDGE_DAEMON_POLL_SECONDS`
  Default: `5`.
- `GEMINI_BRIDGE_DAEMON_MAX_JOBS_PER_TICK`
  Default: `1`.
- `GEMINI_BRIDGE_WORKER_SCRIPT`
  Optional path to `plugins/gemini-bridge/scripts/gemini_bridge_mcp.py`. Defaults to the script in this repository.
- `GEMINI_BIN`
  Default: resolved from PATH and common local install locations.
- `GEMINI_TIMEOUT_SECONDS`
  Default: `360`. This is a no-output timeout, not a total wall-clock timeout.
- `GEMINI_BASE_ARGS`
  Extra CLI args, shell-style string, for example: `--model gemini-3-flash-preview`
- `GEMINI_DEFAULT_MODEL`
  Default: `gemini-3-flash-preview`. Used only when no explicit `--model` is provided.
- `GEMINI_FALLBACK_TO_CLI_DEFAULT_MODEL`
  Default: `1`. If the bridge-added default model fails, retry once without `--model` so the Gemini CLI can use its own current default.
- `GEMINI_AUTH_FILE`
  Optional advanced credential file passed to Gemini as `GOOGLE_APPLICATION_CREDENTIALS`. Do not set this for normal user OAuth unless you know the CLI should use that file.
- `GEMINI_HOME`
  HOME value for the Gemini process. Usually the home directory that contains `.gemini`.
- `GEMINI_CLI_HOME`
  HOME root used by newer Gemini CLI versions. It is the directory that contains `.gemini`, not the `.gemini` directory itself.
- `GEMINI_IDLE_TIMEOUT_SECONDS`
  Deprecated compatibility alias for the no-output timeout.

The MCP bridge uses this invocation by default:

```bash
gemini --approval-mode yolo -p "<prompt>"
```

Fallback attempts are disabled by default. Pass `allow_fallback_strategies: true` only when you explicitly want the bridge to also try `--prompt` and stdin modes.

### MCP Server

- `GEMINI_BIN`
  Default: resolved from PATH and common local install locations.
- `GEMINI_TIMEOUT_SECONDS`
  Default: `360`. This is a no-output timeout, not a total wall-clock timeout.
- `GEMINI_BASE_ARGS`
  Extra CLI args, shell-style string, for example: `--model gemini-3-flash-preview`
- `GEMINI_DEFAULT_MODEL`
  Default: `gemini-3-flash-preview`. Used only when no explicit `--model` is provided.
- `GEMINI_FALLBACK_TO_CLI_DEFAULT_MODEL`
  Default: `1`. If the bridge-added default model fails, retry once without `--model` so the Gemini CLI can use its own current default.
- `GEMINI_HOME`
  HOME value passed to Gemini.
- `GEMINI_AUTH_FILE`
  Optional advanced credential file used as `GOOGLE_APPLICATION_CREDENTIALS`. Normal Gemini CLI user OAuth should usually rely on `GEMINI_HOME`/`HOME` instead.
- `GEMINI_BRIDGE_WORKSPACE`
  Optional target repository root for workflow tools. Use this when the plugin is installed outside the project being worked on.
- `GEMINI_BRIDGE_WORKFLOW_DIR`
  Optional workflow directory relative to the workspace. Defaults to `docs/ai-workflow`.
- `GEMINI_BRIDGE_REGISTRY_FILE`
  Optional platform registry file. Defaults to `~/.codex/gemini-bridge/registry.json`.

## Platform Mode

Platform mode lets one public MCP server manage multiple projects and multiple agents.

Registry:

```text
~/.codex/gemini-bridge/registry.json
```

Recommended flow:

`register_project(projectId, workspace)` -> `register_agent(agentId, role)` -> `Codex writes docs/ai-workflow/tasks/todo/<taskId>.md` -> `notify_task_ready(projectId, taskId)` -> `MCP queues a platform job without starting Gemini` -> `local HTTP bridge daemon starts a Gemini worker` -> `Gemini reads the task document path`

Platform tools:

- `register_project`
- `list_projects`
- `get_project`
- `unregister_project`
- `register_agent`
- `list_agents`
- `notify_task_ready`
- `list_platform_jobs`

`notify_task_ready` records the job and signal, but does not start Gemini. Starting Gemini is handled only by the local HTTP bridge daemon.

## Workflow Mode

This repository uses one workflow pattern only: Codex writes task files, MCP records a file-ready signal, and the async worker dispatches Gemini.

Async worker flow:

`Requirement` -> `Codex writes docs/ai-workflow/tasks/todo/*.md` with `taskId` -> `notify_task_ready` queues a platform job without starting Gemini -> `local HTTP bridge daemon starts a background worker` -> `Gemini CLI reads the fixed task path from disk` -> `docs/ai-workflow/jobs/<job_id>/` is updated continuously -> `Codex reviews result/patch later`

Task document contract:

```markdown
---
taskId: demo
status: todo
phase: implementation
createdAt: 2026-05-18T00:00:00Z
focusFiles:
  - src/example.ts
---

# Task: demo

## Goal

Describe the work Gemini or another worker should perform.
```

The task filename and `taskId` should match, for example `docs/ai-workflow/tasks/todo/demo.md` contains `taskId: demo`. Codex should not send the task body through MCP; it should only call `notify_task_ready` with project and task metadata.

Available async workflow tools:

- `notify_task_ready`
- `list_platform_jobs`
- `get_workflow_job`
- `cancel_workflow_job`

Asynchronous job files:

- `docs/ai-workflow/jobs/<job_id>/status.json`
- `docs/ai-workflow/jobs/<job_id>/events.log`
- `docs/ai-workflow/jobs/<job_id>/stdout.log`
- `docs/ai-workflow/jobs/<job_id>/stderr.log`
- `docs/ai-workflow/jobs/<job_id>/result.md`
- `docs/ai-workflow/jobs/<job_id>/patch.patch`

File-ready signal files:

- `docs/ai-workflow/signals/<signal_id>.json`

The signal JSON contains `taskId` for a single task, `taskIds` for all tasks, `task_files` for repository-relative document paths, and queued jobs. The Gemini prompt contains the task document path, not the task document body.

For cross-project usage, register each project with `register_project`. Workflow artifacts are kept under each project's `docs/ai-workflow` so every development phase can be retained as project documentation.

When the optional HTTP bridge is running, open `http://127.0.0.1:8787/dashboard` to watch job status and log tails. The backend JSON endpoints are `GET /jobs` and `GET /jobs/<job_id>`.

## Reverse Direction: Let Gemini Call An MCP Server

This repository also includes an experimental reverse-direction MCP server:

```text
Gemini CLI -> Gemini-Codex MCP server -> local repository handoff tools
```

Server file:

```text
<repo-root>/mcp/gemini_codex_mcp.py
```

Do not enable this reverse server while testing the forward Codex-to-Gemini bridge. It changes the runtime behavior and can cause `gemini -p` to wait for MCP initialization.

Example registration:

```bash
gemini mcp add \
  --scope project \
  --trust \
  --description "Local MCP server for Gemini to hand work back to Codex" \
  gemini-codex \
  python3 <repo-root>/mcp/gemini_codex_mcp.py
```

Check registration:

```bash
GEMINI_CLI_TRUST_WORKSPACE=true gemini mcp list
```

Then ask Gemini:

```text
Call the ping tool from the gemini-codex MCP server.
```

Available reverse-direction tools:

- `ping`
- `workspace_snapshot`
- `handoff_to_codex`

## Notes

- This implementation uses only Python standard library modules.
- The MCP server returns plain text output from the Gemini CLI, including stdout and stderr sections for debugging.
- The bridge server intentionally avoids shell execution and invokes the CLI with argument arrays.
- The Python MCP servers accept both JSONL stdio messages used by the current MCP SDK and `Content-Length` framed messages used by some older or manual clients.
- Keep project `.gemini/settings.json` free of reverse MCP server entries while testing the forward bridge. If Gemini CLI itself tries to initialize project MCP servers, `gemini -p` can stall before answering.
- If you see `Waiting for MCP servers to initialize...`, run `GEMINI_CLI_TRUST_WORKSPACE=true gemini mcp list`. For the forward bridge architecture, the expected result is usually `No MCP servers configured.`

## Publishing Notes

- Do not publish machine-specific usernames, absolute home paths, or local install paths in public documentation.
- Keep personal overrides in local `.mcp.json`, shell environment, or unpublished setup notes.
- The plugin manifest uses neutral placeholder URLs. Replace them with your real project URLs before marketplace submission.
- Treat this README as product-level documentation; treat your local config as deployment-specific.

# NexusFlow: Codex to AI Agent 桥接器，用异步 worker 把 workflow 任务文件派发给真实的 `gemini` CLI。

主链路：

`Codex App` -> `MCP Client` -> `NexusFlow MCP Server` -> `async worker` -> `gemini -p` -> `docs/ai-workflow/jobs/<job_id>/`

## 目录结构

- `bridge/gemini_bridge_server.py`
  可选的本地 dashboard 服务，用来查看注册中心和异步 job 状态，不直接执行 Gemini。
- `plugins/gemini-bridge/.codex-plugin/plugin.json`
  Codex 插件清单。
- `plugins/gemini-bridge/.mcp.json`
  插件对应的 MCP Server 配置。
- `plugins/gemini-bridge/scripts/gemini_bridge_mcp.py`
  本地 stdio MCP Server，暴露注册中心和异步 worker 工具。
- `mcp/gemini_codex_mcp.py`
  可选的反向 MCP Server，供 Gemini CLI 反过来调用。
- `.agents/plugins/marketplace.json`
  仓库内的 marketplace 配置，方便 Codex 发现这个插件。

## 快速开始

1. 在 Codex App 里加载这个仓库里的本地插件。
2. 先确保 `gemini` CLI 在你真实终端里已经能正常工作，并且已经完成 Google 登录授权。
3. 在本机的 `.mcp.json` 或环境变量里配置 Python 运行时、Gemini CLI 路径和认证信息。
4. 注册项目，在 `docs/ai-workflow/tasks/todo/` 下写任务文件，然后通知本地队列。Dashboard daemon 会在 Codex MCP 调用链之外启动 Gemini worker。

```text
Use NexusFlow: register this project and notify task ready.
```

标准的 stdio MCP 调用不依赖 HTTP Bridge 服务。HTTP 服务只负责 dashboard 和 JSON 状态接口。

## 推荐的本地 MCP 配置

为了便于发布和迁移，建议把机器相关的配置放在你自己的 `.mcp.json` 或环境变量里，而不是写死在公开文档中。

示例：

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "command": "python3",
      "args": [
        "<repo-root>/plugins/gemini-bridge/scripts/gemini_bridge_mcp.py"
      ],
      "cwd": "<repo-root>",
      "env": {
        "GEMINI_BIN": "/path/to/gemini",
        "GEMINI_HOME": "/path/to/home"
      }
    }
  }
}
```

说明：

- `GEMINI_BIN` 应该指向你终端里实际可用的 Gemini CLI。
- `GEMINI_HOME` / `GEMINI_CLI_HOME` 应该指向包含 `.gemini/` 的 home 目录，例如 `/Users/you`，不是 `/Users/you/.gemini`。
- 如果误传了 `.gemini` 目录，bridge 会在启动 Gemini CLI 前自动归一化到它的父目录。
- 普通 Gemini CLI 用户 OAuth 不建议设置 `GEMINI_AUTH_FILE`。CLI 通常会从 `HOME/.gemini` 或自己的 keychain 存储读取登录状态。
- 如果本机环境已经配置好了，也可以省略这些变量，让 bridge 自行探测。

## MCP 工具

只暴露异步 worker 平台工具：

- `register_project`
- `list_projects`
- `get_project`
- `unregister_project`
- `register_agent`
- `list_agents`
- `notify_task_ready`
- `list_platform_jobs`
- `get_workflow_job`
- `cancel_workflow_job`

默认模型：

- bridge 默认使用 `gemini-3-flash-preview`。
- 如果 `gemini_args` 或 `GEMINI_BASE_ARGS` 里已经显式传了 `--model`，则优先使用显式值。
- 也可以通过 `GEMINI_DEFAULT_MODEL` 覆盖默认模型。

直接 prompt 执行和派发工具都不暴露给 Codex。Codex 只能入队和读取状态；真正启动 Gemini worker 的动作由本地 HTTP bridge daemon 负责。

## HTTP Bridge API

### 健康检查

```bash
curl http://127.0.0.1:8787/health
```

### Dashboard

```bash
open http://127.0.0.1:8787/dashboard
```

控制台是 HTTP Bridge 内置的中文单页页面，不需要单独的前端构建流程；部署 `bridge/gemini_bridge_server.py` 时会一起提供页面。

`/gemini` HTTP 执行入口已移除。请使用 MCP 异步 worker 工具。

### 本地 Daemon

Dashboard 后端可以消费 queued platform job，并在本机启动 Gemini worker。这个动作刻意放在 Codex MCP 调用链之外。

```bash
open http://127.0.0.1:8787/dashboard
```

可以在 dashboard 里启动/停止 daemon，也可以用环境变量启动自动消费：

```bash
GEMINI_BRIDGE_DAEMON_ENABLED=1 python3 bridge/gemini_bridge_server.py
```

如果要作为本地后台进程常驻，使用：

```bash
GEMINI_BRIDGE_WORKSPACE=/path/to/project scripts/start_gemini_bridge_server.sh
scripts/stop_gemini_bridge_server.sh
```

启动脚本会打印控制台地址、健康检查地址和日志路径。需要指定 Python 时可以传 `PYTHON_BIN=/path/to/python3`。

macOS 上如果希望服务不随终端/会话退出，优先使用 LaunchAgent：

```bash
GEMINI_BRIDGE_WORKSPACE=/path/to/project scripts/install_gemini_bridge_launch_agent.sh
scripts/uninstall_gemini_bridge_launch_agent.sh
```

## 环境变量

### Bridge Server

- `GEMINI_BRIDGE_HOST`
  默认：`127.0.0.1`
- `GEMINI_BRIDGE_PORT`
  默认：`8787`
- `GEMINI_BRIDGE_DAEMON_ENABLED`
  默认：`1`。只有在你想暂停自动消费，并从 dashboard 手动派发 queued jobs 时才设为 `0`。
- `GEMINI_BRIDGE_DAEMON_POLL_SECONDS`
  默认：`5`。
- `GEMINI_BRIDGE_DAEMON_MAX_JOBS_PER_TICK`
  默认：`1`。
- `GEMINI_BRIDGE_WORKER_SCRIPT`
  可选，指向 `plugins/gemini-bridge/scripts/gemini_bridge_mcp.py`。默认使用当前仓库里的脚本。
- `GEMINI_BIN`
  默认：从 PATH 和常见本地安装位置中解析。
- `GEMINI_TIMEOUT_SECONDS`
  默认：`360`。这是“无输出超时”，不是总运行时长超时。
- `GEMINI_BASE_ARGS`
  额外 CLI 参数，shell 风格字符串，例如：`--model gemini-3-flash-preview`
- `GEMINI_DEFAULT_MODEL`
  默认：`gemini-3-flash-preview`。只有在没有显式传入 `--model` 时才会使用。
- `GEMINI_FALLBACK_TO_CLI_DEFAULT_MODEL`
  默认：`1`。如果 bridge 自动附加的默认模型失败，会不带 `--model` 重试一次，让 Gemini CLI 使用它自己的当前默认模型。
- `GEMINI_AUTH_FILE`
  高级配置：作为 `GOOGLE_APPLICATION_CREDENTIALS` 传给 Gemini 的认证文件。普通用户 OAuth 通常不要设置它。
- `GEMINI_HOME`
  Gemini 进程使用的 HOME，通常是包含 `.gemini` 的 home 目录。
- `GEMINI_CLI_HOME`
  新版 Gemini CLI 使用的 HOME 根目录。它应该是包含 `.gemini` 的目录，不是 `.gemini` 目录本身。
- `GEMINI_IDLE_TIMEOUT_SECONDS`
  已废弃，作为无输出超时的兼容别名保留。

MCP bridge 默认调用方式：

```bash
gemini --approval-mode yolo -p "<prompt>"
```

默认不会启用 fallback。只有你明确需要时，才传 `allow_fallback_strategies: true` 去尝试 `--prompt` 和 stdin 模式。

### MCP Server

- `GEMINI_BIN`
  默认：从 PATH 和常见本地安装位置中解析。
- `GEMINI_TIMEOUT_SECONDS`
  默认：`360`。这是“无输出超时”，不是总运行时长超时。
- `GEMINI_BASE_ARGS`
  额外 CLI 参数，shell 风格字符串，例如：`--model gemini-3-flash-preview`
- `GEMINI_DEFAULT_MODEL`
  默认：`gemini-3-flash-preview`。只有在没有显式传入 `--model` 时才会使用。
- `GEMINI_FALLBACK_TO_CLI_DEFAULT_MODEL`
  默认：`1`。如果 bridge 自动附加的默认模型失败，会不带 `--model` 重试一次，让 Gemini CLI 使用它自己的当前默认模型。
- `GEMINI_HOME`
  传给 Gemini 的 HOME。
- `GEMINI_AUTH_FILE`
  高级配置：作为 `GOOGLE_APPLICATION_CREDENTIALS` 使用。普通 Gemini CLI 用户 OAuth 通常依赖 `GEMINI_HOME`/`HOME`。
- `GEMINI_BRIDGE_WORKSPACE`
  可选，workflow 工具操作的目标仓库根目录。插件安装目录和实际项目不一致时使用。
- `GEMINI_BRIDGE_WORKFLOW_DIR`
  可选，相对于目标仓库的 workflow 目录。默认是 `docs/ai-workflow`。
- `GEMINI_BRIDGE_REGISTRY_FILE`
  可选，平台注册中心文件。默认是 `~/.codex/gemini-bridge/registry.json`。

## Platform 模式

Platform 模式让一个公共 MCP Server 管理多个项目和多个 Agent。

注册中心：

```text
~/.codex/gemini-bridge/registry.json
```

推荐链路：

`register_project(projectId, workspace)` -> `register_agent(agentId, role)` -> `Codex 写 docs/ai-workflow/tasks/todo/<taskId>.md` -> `notify_task_ready(projectId, taskId)` -> `MCP 只创建平台 job，不启动 Gemini` -> `本地 HTTP bridge daemon 启动 Gemini worker` -> `Gemini 根据任务文档路径读取任务`

平台工具：

- `register_project`
- `list_projects`
- `get_project`
- `unregister_project`
- `register_agent`
- `list_agents`
- `notify_task_ready`
- `list_platform_jobs`

`notify_task_ready` 只登记 job 和 signal，不启动 Gemini。启动 Gemini 只由本地 HTTP bridge daemon 负责。

## Workflow 模式

这个仓库只保留一种 workflow：Codex 写任务文件，MCP 记录文件写完信号，异步 worker 再派发 Gemini。

异步 worker 链路：

`需求` -> `Codex 写 docs/ai-workflow/tasks/todo/*.md`，并写入 `taskId` -> `notify_task_ready` 只排队 platform job，不启动 Gemini -> `本地 HTTP bridge daemon 启动后台 worker` -> `Gemini CLI 根据固定任务文档路径从磁盘读取任务` -> `docs/ai-workflow/jobs/<job_id>/` 持续更新 -> `Codex 稍后审核 result/patch`

任务文档约定：

```markdown
---
taskId: demo
status: todo
phase: implementation
createdAt: 2026-05-18T00:00:00Z
focusFiles:
  - src/example.ts
---

# Task: demo

## Goal

描述 Gemini 或其他 worker 应该完成的工作。
```

任务文件名和 `taskId` 应保持一致，例如 `docs/ai-workflow/tasks/todo/demo.md` 中写 `taskId: demo`。Codex 不通过 MCP 发送任务正文，只调用 `notify_task_ready` 并传项目和任务元数据。

可用异步 workflow 工具：

- `notify_task_ready`
- `list_platform_jobs`
- `get_workflow_job`
- `cancel_workflow_job`

异步 job 文件：

- `docs/ai-workflow/jobs/<job_id>/status.json`
- `docs/ai-workflow/jobs/<job_id>/events.log`
- `docs/ai-workflow/jobs/<job_id>/stdout.log`
- `docs/ai-workflow/jobs/<job_id>/stderr.log`
- `docs/ai-workflow/jobs/<job_id>/result.md`
- `docs/ai-workflow/jobs/<job_id>/patch.patch`

文件写完信号文件：

- `docs/ai-workflow/signals/<signal_id>.json`

signal JSON 会包含单任务的 `taskId`、全量任务的 `taskIds`、仓库相对路径 `task_files`，以及排队的 job。Gemini prompt 只包含任务文档路径，不包含任务文档正文。

跨项目使用时，每个项目通过 `register_project` 注册。开发文件默认保留在各项目的 `docs/ai-workflow`，便于作为每个开发阶段的文档沉淀。

如果启动了可选 HTTP bridge，可以打开 `http://127.0.0.1:8787/dashboard` 查看 job 状态和日志尾部。后端 JSON 接口是 `GET /jobs` 和 `GET /jobs/<job_id>`。

## 反向模式：让 Gemini 调 MCP

仓库里还包含一个实验性的反向 MCP Server：

```text
Gemini CLI -> Gemini-Codex MCP server -> 本地仓库交接工具
```

服务文件：

```text
<repo-root>/mcp/gemini_codex_mcp.py
```

测试正向的 Codex -> Gemini bridge 时，不要同时启用这个反向 server。否则会改变运行形态，并可能导致 `gemini -p` 卡在 MCP 初始化阶段。

示例注册命令：

```bash
gemini mcp add \
  --scope project \
  --trust \
  --description "Local MCP server for Gemini to hand work back to Codex" \
  gemini-codex \
  python3 <repo-root>/mcp/gemini_codex_mcp.py
```

检查注册结果：

```bash
GEMINI_CLI_TRUST_WORKSPACE=true gemini mcp list
```

然后可以让 Gemini 调用：

```text
Call the ping tool from the gemini-codex MCP server.
```

反向模式可用工具：

- `ping`
- `workspace_snapshot`
- `handoff_to_codex`

## 说明

- 整个实现只依赖 Python 标准库。
- MCP Server 会返回 Gemini CLI 的纯文本输出，并带有 stdout 和 stderr 分段，便于排查。
- HTTP bridge 不走 shell，而是直接用参数数组调用 CLI。
- 这些 Python MCP Server 同时兼容当前 MCP SDK 常用的 JSONL stdio 消息格式，以及部分旧客户端使用的 `Content-Length` framing。
- 测试正向 bridge 时，建议保持项目 `.gemini/settings.json` 中没有反向 MCP server 配置，否则 Gemini CLI 自己初始化项目 MCP 时可能会卡住。
- 如果看到 `Waiting for MCP servers to initialize...`，可以执行 `GEMINI_CLI_TRUST_WORKSPACE=true gemini mcp list`。对于正向 bridge 架构，通常期望看到 `No MCP servers configured.`

## 发布说明

- 公开文档里不要保留机器专属用户名、绝对 home 路径或本地安装路径。
- 个人配置请放在本地 `.mcp.json`、shell 环境变量或不公开的部署说明里。
- `plugin.json` 里的 URL 现在是中性占位值，正式上架前请替换成真实项目地址。
- 这个 README 应该保持为公共产品文档，本机差异放到部署层处理。
