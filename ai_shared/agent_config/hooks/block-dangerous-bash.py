#!/usr/bin/env python3
"""阻断与 Git 分支流程无关的明显危险 shell 命令。"""

from __future__ import annotations

import json
import re
import sys


def extract_command(payload: str) -> str:
    """从 hook payload 中提取 shell 命令。"""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return payload

    tool_input = data.get("tool_input") or data.get("input") or {}
    if not isinstance(tool_input, dict):
        return payload

    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command)


def deny(command: str) -> int:
    """输出结构化 deny。"""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"危险命令被拦截: {command}",
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    """检查危险 shell 命令。"""
    command = extract_command(sys.stdin.read())
    patterns = (
        r"\brm\s+-rf\b",
        r"\bsudo\b",
        r"\bchmod\s+-R\s+777\b",
        r"\bcat\s+\.env\b",
        r"\bcat\s+.*secret",
        r"\bcat\s+.*credential",
    )
    for pattern in patterns:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return deny(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
