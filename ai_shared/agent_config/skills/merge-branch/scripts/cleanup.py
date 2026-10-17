#!/usr/bin/env python3
"""Delete merged PR remote branch on explicit cleanup command; never delete local branches."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["remote"])
    parser.add_argument("--pr", type=int, required=True)
    args = parser.parse_args()
    pr = json.loads(
        run(
            "gh",
            "pr",
            "view",
            str(args.pr),
            "--json",
            "state,baseRefName,headRefName,headRepositoryOwner,"
            "headRepository,mergeCommit,mergedAt",
        )
    )
    branch = pr["headRefName"]
    if (
        pr["state"] != "MERGED"
        or pr["baseRefName"] != "main"
        or not pr.get("mergedAt")
        or not pr.get("mergeCommit")
        or branch in ("main", "master")
    ):
        raise RuntimeError("PR 合并状态或分支信息不符合清理条件")
    repo = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner"))[
        "nameWithOwner"
    ]
    head_owner = (pr.get("headRepositoryOwner") or {}).get("login")
    if head_owner != repo.split("/")[0]:
        raise RuntimeError("外部 Fork 分支不允许自动清理")
    remote_url = run("git", "remote", "get-url", "origin")
    # Do not delete arbitrary remote refs; verify current repository identity through gh.
    if repo.lower() not in remote_url.lower().replace(":", "/"):
        raise RuntimeError("origin 与当前 GitHub 仓库无法核对")
    result = subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "删除远程分支失败")
    print(
        json.dumps(
            {"state": "CLEANED", "pr": args.pr, "remote_branch": branch},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        print(
            json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)
