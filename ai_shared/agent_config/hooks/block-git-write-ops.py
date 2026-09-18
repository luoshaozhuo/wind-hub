#!/usr/bin/env python3
"""阻断默认不允许的 Git/GitHub 写操作。"""

from __future__ import annotations

import json
import re
import sys


DENIED_GIT_PATTERNS = (
    r"\bgit\s+commit(?:\s|$)",
    r"\bgit\s+push(?:\s|$)",
    r"\bgit\s+reset(?:\s|$)",
    r"\bgit\s+clean(?:\s|$)",
    r"\bgit\s+checkout\s+--(?:\s|$)",
    r"\bgit\s+restore(?:\s|$)",
    r"\bgit\s+switch(?:\s|$)",
    r"\bgit\s+merge(?:\s|$)",
    r"\bgit\s+rebase(?:\s|$)",
    r"\bgit\s+cherry-pick(?:\s|$)",
    r"\bgit\s+tag(?:\s|$)",
    r"\bgit\s+stash\s+(?:pop|drop|clear)(?:\s|$)",
    r"\bgit\s+remote\s+set-url(?:\s|$)",
)

DENIED_GH_PATTERNS = (
    r"\bgh\s+pr\s+(?:merge|close|edit|review)(?:\s|$)",
    r"\bgh\s+repo\s+(?:rename|edit|delete|archive)(?:\s|$)",
    r"\bgh\s+release\s+(?:create|delete|edit)(?:\s|$)",
)


def extract_command(payload: str) -> str:
    """从 hook payload 中提取 shell 命令文本。"""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return payload
    tool_input = data.get("tool_input") or data.get("input") or {}
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or tool_input.get("cmd") or ""
        if isinstance(command, list):
            return " ".join(str(part) for part in command)
        return str(command)
    return payload


def deny(message: str) -> int:
    """输出统一 deny payload。"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }, ensure_ascii=False))
    return 0


def main() -> int:
    """阻断默认不允许的 Git/GitHub 写操作。"""
    command = extract_command(sys.stdin.read()).strip()
    if not command:
        return 0

    for pattern in DENIED_GIT_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return deny(f"默认禁止自动执行 Git 写操作: {command}")

    for pattern in DENIED_GH_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return deny(f"默认禁止自动执行 GitHub 写操作: {command}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
