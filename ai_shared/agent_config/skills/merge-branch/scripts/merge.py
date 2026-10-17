#!/usr/bin/env python3
"""Validate and squash-merge current task PR without switching local branches."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


class Blocked(RuntimeError):
    pass


def command(*args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode and not allow_failure:
        raise Blocked(f"{' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def gh_json(*args: str) -> Any:
    return json.loads(command("gh", *args))


def actions_disabled() -> bool:
    """仓库级 GitHub Actions 是否被显式禁用。

    仅以 API 确证的 ``enabled == false`` 为 True；查询失败或结果不明
    抛 Blocked（UNKNOWN 不得作为放行依据）。
    """
    repo = command(
        "gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
    )
    data = gh_json("api", f"repos/{repo}/actions/permissions")
    if data.get("enabled") is False:
        return True
    if data.get("enabled") is True:
        return False
    raise Blocked(f"无法确认 GitHub Actions 启用状态: {data}")


def inspect(pr_number: int, *, allow_no_ci: bool = False) -> dict[str, Any]:
    branch = command("git", "branch", "--show-current")
    if not branch or branch in ("main", "master"):
        raise Blocked("必须在任务分支上运行")
    if command("git", "status", "--porcelain"):
        raise Blocked("工作区或暂存区不干净")
    command("git", "fetch", "origin", "--prune")
    if command("git", "rev-parse", "HEAD") != command("git", "rev-parse", f"origin/{branch}"):
        raise Blocked("本地与远程任务分支未同步")

    pr = gh_json("pr", "view", str(pr_number), "--json", "number,state,isDraft,headRefName,baseRefName,headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,url,mergedAt,mergeCommit")
    if pr["headRefName"] != branch or pr["baseRefName"] != "main":
        raise Blocked("PR 源分支或目标分支与当前任务不符")
    if pr["state"] == "MERGED":
        return {"state": "MERGED", "pr": pr}
    if pr["state"] != "OPEN" or pr["isDraft"]:
        raise Blocked("PR 未开放或仍为 Draft")
    if pr["headRefOid"] != command("git", "rev-parse", "HEAD"):
        raise Blocked("PR HEAD 与本地提交不一致")
    if pr["mergeable"] != "MERGEABLE" or pr["mergeStateStatus"] != "CLEAN":
        raise Blocked(f"GitHub 尚未确认允许合并: {pr['mergeable']}/{pr['mergeStateStatus']}")
    if pr["reviewDecision"] in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
        raise Blocked(f"代码审查未满足要求: {pr['reviewDecision']}")

    checks = pr.get("statusCheckRollup") or []
    ci_state = "PASS"
    if not checks:
        # 默认：无检查结果绝不视作通过。仅当调用方显式 --allow-no-ci，
        # 且 API 确证仓库 Actions 被整体禁用（CI 设施关闭，而非检查未触发/
        # 未完成）时降级放行，并如实标注 NOT_RUN，不伪装为 CI PASS。
        if not allow_no_ci:
            raise Blocked(
                "没有 CI 检查结果，不能视作通过；若 GitHub Actions 已被仓库"
                "设置整体禁用，可在确认本地 Gate 后显式加 --allow-no-ci"
            )
        if not actions_disabled():
            raise Blocked(
                "GitHub Actions 处于启用状态却没有检查结果（可能未触发或尚未"
                "完成），--allow-no-ci 不适用"
            )
        ci_state = "NOT_RUN_ACTIONS_DISABLED"
    failures = []
    pending = []
    for item in checks:
        name = item.get("name") or item.get("context") or "unnamed"
        conclusion = item.get("conclusion") or item.get("state")
        if conclusion in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            failures.append(name)
        elif conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED"):
            pending.append(f"{name}:{conclusion}")
    if failures or pending:
        raise Blocked(f"CI 未全部完成或存在失败; failed={failures}, pending={pending}")
    if pr["mergeStateStatus"] != "CLEAN":
        raise Blocked("合并状态非 CLEAN，需人工核实 Hook 或特殊仓库门禁")
    return {"state": "READY", "pr": pr, "checks": len(checks), "ci": ci_state}


def verify_merged(pr_number: int) -> dict[str, Any]:
    pr = gh_json("pr", "view", str(pr_number), "--json", "state,mergedAt,mergeCommit,baseRefName,url")
    commit = (pr.get("mergeCommit") or {}).get("oid")
    if pr["state"] != "MERGED" or not pr.get("mergedAt") or not commit or pr["baseRefName"] != "main":
        raise Blocked("GitHub 尚未确认 PR MERGED 且目标为 main")
    command("git", "fetch", "origin", "main")
    proof = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"], check=False)
    if proof.returncode:
        raise Blocked("合并提交尚未验证为 origin/main 祖先")
    return {"state": "MERGED", "pr": pr_number, "squash_sha": commit, "url": pr["url"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("check", "merge"), default="merge")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument(
        "--allow-no-ci",
        action="store_true",
        help="仅当仓库 GitHub Actions 被整体禁用（API 确证）时，允许无检查结果合并",
    )
    args = parser.parse_args()
    info = inspect(args.pr, allow_no_ci=args.allow_no_ci)
    if info["state"] == "MERGED":
        print(json.dumps(verify_merged(args.pr), ensure_ascii=False))
        return 0
    if args.action == "check":
        pr = info["pr"]
        print(json.dumps({"state": "READY", "pr": args.pr, "url": pr["url"], "head_sha": pr["headRefOid"], "ci_checks": info["checks"], "ci": info["ci"]}, ensure_ascii=False))
        return 0

    pr = info["pr"]
    # GitHub enforces branch protection for the ordinary squash merge.
    # Recheck HEAD before merging; never use --admin.
    current = gh_json("pr", "view", str(args.pr), "--json", "headRefOid,baseRefName,state")
    if (current["headRefOid"] != pr["headRefOid"] or
        current["baseRefName"] != "main" or current["state"] != "OPEN"):
        raise Blocked("PR 已发生变化，重新检查后再合并")
    try:
        command("gh", "pr", "merge", str(args.pr), "--squash")
    except Blocked:
        # A timeout or transport error does not imply the merge failed.
        # Verify remote state before deciding whether to report failure.
        result = gh_json("pr", "view", str(args.pr), "--json", "state")
        if result.get("state") != "MERGED":
            raise
    merged = verify_merged(args.pr)
    merged["ci"] = info["ci"]
    print(json.dumps(merged, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Blocked, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
