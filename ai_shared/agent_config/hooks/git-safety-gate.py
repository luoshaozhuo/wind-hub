#!/usr/bin/env python3
"""限制 Coding Agent 的 Git/GitHub 写操作到安全任务分支流程。"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys


PROTECTED_BRANCHES = {"main", "master"}

ALWAYS_DENIED = (
    r"\bgit\s+reset\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+restore\b",
    r"\bgit\s+checkout\b",
    r"\bgit\s+merge\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+cherry-pick\b",
    r"\bgit\s+tag\b",
    r"\bgit\s+stash\s+(?:pop|drop|clear)\b",
    r"\bgit\s+remote\s+set-url\b",
    r"\bgh\s+pr\s+(?:merge|close|edit|review)\b",
    r"\bgh\s+repo\s+(?:rename|edit|delete|archive)\b",
    r"\bgh\s+release\s+(?:create|delete|edit)\b",
)


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


def deny(message: str) -> int:
    """输出统一 deny payload。"""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def current_branch() -> str | None:
    """读取当前分支；detached HEAD 返回 None。"""
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return branch or None


def _contains_force_push(tokens: list[str]) -> bool:
    """判断 push 是否带 force 语义。"""
    return any(
        token in {"-f", "--force", "--force-with-lease", "--delete"}
        or token.startswith("--force=")
        or token.startswith("--force-with-lease=")
        for token in tokens
    )


def _push_targets_protected_branch(tokens: list[str]) -> bool:
    """保守判断 push 参数是否显式指向受保护分支。"""
    for token in tokens:
        normalized = token.removeprefix("+")
        if normalized in PROTECTED_BRANCHES:
            return True
        if ":" in normalized:
            destination = normalized.rsplit(":", 1)[-1]
            destination = destination.removeprefix("refs/heads/")
            if destination in PROTECTED_BRANCHES:
                return True
    return False


def validate_switch(command: str) -> int:
    """只允许从 origin/main 创建新的任务分支。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return deny(f"无法安全解析 git switch 命令: {command}")

    if tokens[:2] != ["git", "switch"] or "-c" not in tokens:
        return deny("仅允许使用 git switch -c <task-branch> origin/main 创建任务分支")

    index = tokens.index("-c")
    if index + 1 >= len(tokens):
        return deny("git switch -c 缺少分支名")

    branch = tokens[index + 1]
    if branch in PROTECTED_BRANCHES:
        return deny(f"禁止创建或切换受保护分支: {branch}")

    if not tokens or tokens[-1] != "origin/main":
        return deny("任务分支必须从最新 origin/main 创建")

    return 0


def validate_commit(command: str) -> int:
    """只允许在普通任务分支 commit。"""
    branch = current_branch()
    if branch is None:
        return deny("detached HEAD 下禁止自动 commit")
    if branch in PROTECTED_BRANCHES:
        return deny(f"禁止直接在受保护分支 {branch} commit")
    return 0


def validate_push(command: str) -> int:
    """只允许非 force 的任务分支 push。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return deny(f"无法安全解析 git push 命令: {command}")

    branch = current_branch()
    if branch is None:
        return deny("detached HEAD 下禁止自动 push")
    if branch in PROTECTED_BRANCHES:
        return deny(f"禁止从受保护分支 {branch} push")
    if _contains_force_push(tokens):
        return deny("禁止 force/delete push")
    if _push_targets_protected_branch(tokens):
        return deny("禁止直接 push 到 main/master")
    return 0


def main() -> int:
    """执行 Git/GitHub 写操作安全检查。"""
    command = extract_command(sys.stdin.read()).strip()
    if not command:
        return 0

    for pattern in ALWAYS_DENIED:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return deny(f"危险 Git/GitHub 写操作被拦截: {command}")

    if re.search(r"\bgit\s+switch\b", command):
        return validate_switch(command)
    if re.search(r"\bgit\s+commit\b", command):
        return validate_commit(command)
    if re.search(r"\bgit\s+push\b", command):
        return validate_push(command)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
