---
taskId: notify-path-mode-test
status: todo
phase: workflow-validation
createdAt: 2026-05-18T00:00:00Z
focusFiles:
  - examples/java/WorkflowModeCheck.java
---

# Task: notify-path-mode-test

## Goal

Validate that `notify_workflow_ready` starts a Gemini job using only a task document path prompt.

## Acceptance Criteria

- The MCP tool call only passes `taskId`.
- The job status records `prompt_mode: task_path`.
- The Gemini stdout shows the task document path in received CLI arguments.
