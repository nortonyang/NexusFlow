# Task: Workspace Docs Mock

Verify that workflow artifacts are stored under `docs/ai-workflow` for the selected workspace.

Expected behavior:

- `start_workflow_job` accepts the target workspace path.
- The background worker runs with that workspace.
- Job status, logs, result, and patch are written under `docs/ai-workflow/jobs/<job_id>/`.
