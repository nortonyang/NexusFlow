#!/usr/bin/env python3
"""Mock Gemini CLI for testing the task-file workflow mode."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    target = Path.cwd() / "examples" / "java" / "WorkflowModeCheck.java"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "public class WorkflowModeCheck {",
                "    public static String message() {",
                '        return "Codex to Gemini workflow mode OK";',
                "    }",
                "",
                "    public static void main(String[] args) {",
                "        System.out.println(message());",
                "    }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print("Mock Gemini consumed the workflow task and created examples/java/WorkflowModeCheck.java")
    print(f"received args: {sys.argv[1:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
