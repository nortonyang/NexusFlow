#!/usr/bin/env python3
"""Local Gemini CLI stand-in for workflow smoke tests."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path.cwd()
    target = repo_root / "examples" / "java" / "HelloGemini.java"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "public class HelloGemini {",
                "    public static void main(String[] args) {",
                '        System.out.println("Hello from Gemini Java workflow!");',
                "    }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print("Mock Gemini created examples/java/HelloGemini.java")
    print(f"argv: {sys.argv[1:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
