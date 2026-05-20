# Demo 任务：Gemini Bridge 工作流调用说明

## 任务 ID

`demo`

## 任务目标

验证通过 Gemini Bridge 触发一个最小开发任务的调用链是否可用，并明确当前调用所需的输入参数、工作区约束和关注文件范围。

## 背景

当前项目主要采用 `docs/development/<task-slug>/` 的文档先行流程。本文件用于补充一个更轻量的 `ai-workflow` 任务入口，便于测试 Gemini Bridge 工作流任务消费能力。

## 任务要求

1. 读取当前仓库中的任务说明文件。
2. 识别任务 ID 为 `demo`。
3. 关注调用时传入的文件范围，优先只查看明确列出的文件。
4. 返回：
   - 任务是否可执行；
   - 当前工作区是否存在必需文件；
   - 如果不可执行，明确阻塞点。

## 关注文件

- `docs/ai-workflow/tasks/todo/demo.md`
- `docs/development/AGENT_WORKFLOW_STATUS.md`

## 约束

- 不做代码修改。
- 不扩大分析范围到未明确列出的业务文件。
- 只输出最小必要结论。

## 预期结果

- Gemini Bridge 能识别该任务。
- 能返回任务状态或阻塞原因。
- 不触发无关代码变更。
