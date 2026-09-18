#!/usr/bin/env python3
"""阻断明显危险的 shell 命令。"""

from __future__ import annotations

import json
import re
import sys


def extract_command(payload: str) -> str:
    """从 hook payload 中尽量提取 shell 命令。"""
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


def main() -> int:
    """检查危险命令。"""
    command = extract_command(sys.stdin.read())
    patterns = [
        r"\brm\s+-rf\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\b",
        r"\bgit\s+push\b",
        r"\bsudo\b",
        r"\bchmod\s+-R\s+777\b",
        r"\bcat\s+\.env\b",
        r"\bcat\s+.*secret",
        r"\bcat\s+.*credential",
    ]
    for pattern in patterns:
        if re.search(pattern, command, flags=re.IGNORECASE):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"危险命令被拦截: {command}",
                }
            }, ensure_ascii=False))
            # Codex 与 Claude Code 都支持“退出 0 + 结构化 deny”。不要同时
            # 返回 2；退出 2 时两端都只应通过 stderr 传递阻断原因。
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
