#!/usr/bin/env python3
"""Direct squash merge current task branch to main without PR or test gates."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


class BlockedError(RuntimeError):
    """Unsafe or unavailable merge operation."""


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise BlockedError(
            f"{' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def is_ancestor(older: str, newer: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def merge() -> dict[str, str]:
    branch = run("git", "branch", "--show-current")
    if not branch or branch in {"main", "master"}:
        raise BlockedError("必须在任务分支执行")
    if run("git", "status", "--porcelain"):
        raise BlockedError("工作区不干净")

    run("git", "fetch", "origin", "main", branch)
    head = run("git", "rev-parse", "HEAD")
    if head != run("git", "rev-parse", f"refs/remotes/origin/{branch}"):
        raise BlockedError("当前 HEAD 与远程任务分支不一致")
    base = run("git", "rev-parse", "refs/remotes/origin/main")
    if not is_ancestor(base, head):
        raise BlockedError("任务分支不包含最新 main，不能直接 Squash Merge")

    tree = run("git", "rev-parse", f"{head}^{{tree}}")
    squash = run(
        "git", "commit-tree", tree, "-p", base,
        "-m", f"feat: squash merge {branch} into main",
    )
    # 非强制推送；若服务端分支保护拒绝，源分支保持不变。
    run("git", "push", "origin", f"{squash}:refs/heads/main")
    run("git", "fetch", "origin", "main")
    if run("git", "rev-parse", "refs/remotes/origin/main") != squash:
        raise BlockedError("远程 main 未确认指向 Squash Commit，禁止清理分支")

    result: dict[str, str] = {
        "state": "MERGED",
        "source_branch": branch,
        "source_sha": head,
        "previous_main": base,
        "squash_sha": squash,
    }
    try:
        if run("git", "status", "--porcelain"):
            raise BlockedError("合并后工作区变化，停止本地清理")
        worktrees = run("git", "worktree", "list", "--porcelain")
        if "branch refs/heads/main" in worktrees.splitlines():
            raise BlockedError("main 被另一 worktree 使用")
        current_main = run("git", "rev-parse", "refs/heads/main")
        if not is_ancestor(current_main, squash):
            raise BlockedError("本地 main 已分叉，禁止覆盖")
        run("git", "switch", "main")
        run("git", "merge", "--ff-only", "origin/main")
        if run("git", "rev-parse", "HEAD") != squash:
            raise BlockedError("本地 main 未同步")
        run("git", "branch", "-D", branch)
        result["local_branch"] = "deleted"
        # 只删除已确认内容与合并前来源 HEAD 一致的远程分支。
        run("git", "fetch", "origin", branch)
        if run("git", "rev-parse", f"refs/remotes/origin/{branch}") != head:
            raise BlockedError("远程源分支已变化，拒绝删除")
        run("git", "push", "origin", "--delete", branch)
        result["remote_branch"] = "deleted"
        result["state"] = "MERGED_AND_CLEANED"
    except BlockedError as exc:
        result["state"] = "MERGED_CLEANUP_BLOCKED"
        result["cleanup_error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("merge",), nargs="?", default="merge")
    parser.parse_args()
    print(json.dumps(merge(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BlockedError as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
