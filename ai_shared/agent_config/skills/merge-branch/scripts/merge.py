#!/usr/bin/env python3
"""Validate and squash-merge current task PR then synchronize local main and clean up."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class BlockedError(RuntimeError):
    pass


def command(*args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BlockedError(f"{' '.join(args)}: {detail}")
    return result.stdout.strip()


def gh_json(*args: str) -> Any:
    return json.loads(command("gh", *args))


PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED"}
FAIL_CONCLUSIONS = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
}


def gh_api(path: str) -> Any:
    """GET a GitHub REST endpoint.

    Returns parsed JSON, None when the resource is absent (HTTP 404), and
    raises BlockedError for any transport failure or other error status so an
    unknown CI state can never be mistaken for "no checks".
    """
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    try:
        status = str(json.loads(result.stdout).get("status"))
    except (json.JSONDecodeError, ValueError):
        status = ""
    if status == "404":
        return None
    detail = result.stderr.strip() or result.stdout.strip()
    raise BlockedError(f"gh api {path}: {detail}")


def repo_slug() -> str:
    return str(gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"])


def required_contexts(slug: str, base: str) -> set[str]:
    """Effective required status-check contexts from classic protection and rulesets."""
    contexts: set[str] = set()
    classic = gh_api(f"repos/{slug}/branches/{base}/protection/required_status_checks")
    if isinstance(classic, dict):
        contexts.update(str(c) for c in (classic.get("contexts") or []))
        for entry in classic.get("checks") or []:
            if isinstance(entry, dict) and entry.get("context"):
                contexts.add(str(entry["context"]))
    rules = gh_api(f"repos/{slug}/rules/branches/{base}")
    if isinstance(rules, list):
        for rule in rules:
            if rule.get("type") != "required_status_checks":
                continue
            params = rule.get("parameters") or {}
            for entry in params.get("required_status_checks") or []:
                if isinstance(entry, str):
                    contexts.add(entry)
                elif isinstance(entry, dict) and entry.get("context"):
                    contexts.add(str(entry["context"]))
            contexts.update(str(c) for c in (params.get("contexts") or []))
    return contexts


def _check_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("context") or "unnamed")


def _check_result(item: dict[str, Any]) -> Any:
    return item.get("conclusion") or item.get("state")


def evaluate_ci(pr: dict[str, Any], slug: str) -> str:
    """Decide CI readiness from the repo-level Actions switch and effective rules.

    Returns "PASS" (Actions enabled, all checks green) or
    "NOT_RUN_ACTIONS_DISABLED" (Actions explicitly disabled at repo level and
    no external required check unmet). Everything else blocks.
    """
    checks = pr.get("statusCheckRollup") or []
    permissions = gh_api(f"repos/{slug}/actions/permissions")
    if not isinstance(permissions, dict) or not isinstance(
        permissions.get("enabled"), bool
    ):
        raise BlockedError("无法确认 GitHub Actions 仓库级开关状态（enabled 未知），禁止合并")
    actions_enabled: bool = permissions["enabled"]

    if actions_enabled:
        if not checks:
            raise BlockedError("GitHub Actions 已启用但当前 PR 没有任何 Check，禁止合并")
        failures: list[str] = []
        pending: list[str] = []
        for item in checks:
            name = _check_name(item)
            result = _check_result(item)
            if result in FAIL_CONCLUSIONS:
                failures.append(name)
            elif result not in PASS_CONCLUSIONS:
                pending.append(f"{name}:{result}")
        if failures or pending:
            raise BlockedError(
                f"CI 未全部完成或存在失败; failed={failures}, pending={pending}"
            )
        return "PASS"

    # Actions explicitly disabled: only external required checks may block.
    results = {_check_name(item): _check_result(item) for item in checks}
    unmet = [c for c in required_contexts(slug, pr["baseRefName"])
             if results.get(c) not in PASS_CONCLUSIONS]
    if unmet:
        raise BlockedError(f"Actions 已禁用，但存在未满足的外部必需检查: {unmet}")
    return "NOT_RUN_ACTIONS_DISABLED"


def inspect(pr_number: int) -> dict[str, Any]:
    branch = command("git", "branch", "--show-current")
    if not branch or branch in ("main", "master"):
        raise BlockedError("必须在任务分支上运行")
    if command("git", "status", "--porcelain"):
        raise BlockedError("工作区或暂存区不干净")
    command("git", "fetch", "origin", "main", branch)
    if command("git", "rev-parse", "HEAD") != command(
        "git", "rev-parse", f"origin/{branch}"
    ):
        raise BlockedError("本地与远程任务分支未同步")

    pr = gh_json(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "number,state,isDraft,headRefName,baseRefName,headRefOid,mergeable,"
        "mergeStateStatus,reviewDecision,statusCheckRollup,url,mergedAt,mergeCommit",
    )
    if pr["headRefName"] != branch or pr["baseRefName"] != "main":
        raise BlockedError("PR 源分支或目标分支与当前任务不符")
    if pr["state"] == "MERGED":
        return {"state": "MERGED", "pr": pr}
    if pr["state"] != "OPEN" or pr["isDraft"]:
        raise BlockedError("PR 未开放或仍为 Draft")
    if pr["headRefOid"] != command("git", "rev-parse", "HEAD"):
        raise BlockedError("PR HEAD 与本地提交不一致")
    if pr["mergeable"] != "MERGEABLE" or pr["mergeStateStatus"] != "CLEAN":
        raise BlockedError(
            f"GitHub 尚未确认允许合并: {pr['mergeable']}/{pr['mergeStateStatus']}"
        )
    if pr["reviewDecision"] in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
        raise BlockedError(f"代码审查未满足要求: {pr['reviewDecision']}")

    checks = pr.get("statusCheckRollup") or []
    ci_state = evaluate_ci(pr, repo_slug())
    if pr["mergeStateStatus"] != "CLEAN":
        raise BlockedError("合并状态非 CLEAN，需人工核实 Hook 或特殊仓库门禁")
    return {"state": "READY", "pr": pr, "checks": len(checks), "ci": ci_state}


def verify_merged(pr_number: int) -> dict[str, Any]:
    pr = gh_json(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "state,mergedAt,mergeCommit,baseRefName,url",
    )
    commit = (pr.get("mergeCommit") or {}).get("oid")
    if (
        pr["state"] != "MERGED"
        or not pr.get("mergedAt")
        or not commit
        or pr["baseRefName"] != "main"
    ):
        raise BlockedError("GitHub 尚未确认 PR MERGED 且目标为 main")
    command("git", "fetch", "origin", "main")
    proof = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "origin/main"], check=False
    )
    if proof.returncode:
        raise BlockedError("合并提交尚未验证为 origin/main 祖先")
    return {"state": "MERGED", "pr": pr_number, "squash_sha": commit, "url": pr["url"]}



def finalize_local(pr: dict[str, Any], squash_sha: str) -> dict[str, Any]:
    """Fast-forward local main and remove the verified merged task branch."""
    branch = str(pr["headRefName"])
    head_sha = str(pr["headRefOid"])
    if not branch or branch in {"main", "master"}:
        raise BlockedError("不允许清理主分支")
    if command("git", "branch", "--show-current") != branch:
        raise BlockedError("当前任务分支已变化")
    if command("git", "status", "--porcelain"):
        raise BlockedError("工作区不干净")
    if command("git", "rev-parse", "HEAD") != head_sha:
        raise BlockedError("任务分支 HEAD 与合并的 PR 不一致")
    command("git", "fetch", "origin", "main")
    proof = subprocess.run(
        ["git", "merge-base", "--is-ancestor", squash_sha, "origin/main"],
        check=False,
    )
    if proof.returncode:
        raise BlockedError("Squash Commit 未进入 origin/main")
    old_main = command("git", "rev-parse", "refs/heads/main")
    ff = subprocess.run(
        ["git", "merge-base", "--is-ancestor", old_main, "origin/main"],
        check=False,
    )
    if ff.returncode:
        raise BlockedError("本地 main 已分叉，禁止覆盖")
    worktrees = command("git", "worktree", "list", "--porcelain")
    if "branch refs/heads/main" in worktrees.splitlines():
        raise BlockedError("main 正被其他 worktree 使用")
    command("git", "switch", "main")
    command("git", "merge", "--ff-only", "origin/main")
    local_head = command("git", "rev-parse", "HEAD")
    if local_head != command("git", "rev-parse", "origin/main"):
        raise BlockedError("本地 main 同步失败")
    if command("git", "rev-parse", f"refs/heads/{branch}") != head_sha:
        raise BlockedError("任务分支引用已变化，拒绝删除")
    command("git", "branch", "-D", branch)
    return {"local_branch": "main", "local_head": local_head,
            "deleted_local_branch": branch}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("check", "merge"), default="merge")
    parser.add_argument("--pr", type=int, help="不指定时自动识别当前分支 PR")
    args = parser.parse_args()
    if args.pr is None:
        current_pr = gh_json("pr", "view", "--json", "number")
        args.pr = int(current_pr["number"])
    info = inspect(args.pr)
    if info["state"] == "MERGED":
        merged = verify_merged(args.pr)
        if args.action == "merge":
            try:
                merged.update(finalize_local(info["pr"], merged["squash_sha"]))
                merged["state"] = "MERGED_AND_CLEANED"
            except BlockedError as exc:
                merged["state"] = "MERGED_LOCAL_CLEANUP_BLOCKED"
                merged["local_cleanup_error"] = str(exc)
        print(json.dumps(merged, ensure_ascii=False))
        return 0
    if args.action == "check":
        pr = info["pr"]
        report = {
            "state": "READY",
            "pr": args.pr,
            "url": pr["url"],
            "head_sha": pr["headRefOid"],
            "ci_checks": info["checks"],
            "ci": info["ci"],
        }
        print(json.dumps(report, ensure_ascii=False))
        return 0


    pr = info["pr"]
    started = time.perf_counter()
    base_sha = command("git", "rev-parse", "origin/main")
    changed = command("git", "diff", "--name-only", "origin/main...HEAD").splitlines()
    gate_script = Path(__file__).resolve().parents[5] / "scripts" / "ci_gate.py"
    if not gate_script.is_file():
        raise BlockedError(f"合并验证执行器不存在: {gate_script}")
    with tempfile.TemporaryDirectory(prefix="wind-hub-merge-") as temp_dir:
        changed_file = Path(temp_dir) / "changed.txt"
        changed_file.write_text("\n".join(changed) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(gate_script), "merge", "--file-list", str(changed_file)],
            check=False,
        )
        if result.returncode:
            raise BlockedError(f"受影响范围验证失败: exit={result.returncode}")
    if command("git", "status", "--porcelain"):
        raise BlockedError("验证过程产生未提交修改；停止合并")
    if command("git", "rev-parse", "HEAD") != pr["headRefOid"]:
        raise BlockedError("本地 HEAD 在验证过程中发生变化")
    command("git", "fetch", "origin", "main")
    if command("git", "rev-parse", "origin/main") != base_sha:
        raise BlockedError("main 在验证过程中更新，需重新验证")
    validation_elapsed = round(time.perf_counter() - started, 3)
    # 本地验收期间 CI、审查和分支都可能改变；重新执行完整远端门禁。
    current_info = inspect(args.pr)
    current = current_info["pr"]
    if (
        current_info["state"] != "READY"
        or current["headRefName"] != pr["headRefName"]
        or current["headRefOid"] != pr["headRefOid"]
        or current["baseRefName"] != "main"
        or current["state"] != "OPEN"
    ):
        raise BlockedError("PR 已发生变化，重新检查后再合并")
    if command("git", "rev-parse", "origin/main") != base_sha:
        raise BlockedError("main 在最终检查期间更新，需重新验证")
    try:
        # GitHub 原子校验 HEAD，封住最终查询与合并请求之间的提交变化窗口。
        command(
            "gh", "pr", "merge", str(args.pr), "--squash",
            "--match-head-commit", pr["headRefOid"],
        )
    except BlockedError:
        # A timeout or transport error does not imply the merge failed.
        # Verify remote state before deciding whether to report failure.
        result = gh_json("pr", "view", str(args.pr), "--json", "state")
        if result.get("state") != "MERGED":
            raise
    merged = verify_merged(args.pr)
    merged["ci"] = current_info["ci"]
    merged["validated_head_sha"] = pr["headRefOid"]
    merged["validated_base_sha"] = base_sha
    merged["local_validation_seconds"] = validation_elapsed
    try:
        merged.update(finalize_local(pr, merged["squash_sha"]))
        merged["state"] = "MERGED_AND_CLEANED"
    except BlockedError as exc:
        merged["state"] = "MERGED_LOCAL_CLEANUP_BLOCKED"
        merged["local_cleanup_error"] = str(exc)
    print(json.dumps(merged, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BlockedError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"state": "BLOCKED", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)
