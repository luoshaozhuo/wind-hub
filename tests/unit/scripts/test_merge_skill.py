"""merge.py 合并安全条件测试。

git/gh/subprocess 全部被替身接管，不会对真实仓库或 PR 发起任何写操作。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
MERGE = (
    ROOT
    / "ai_shared/agent_config/skills/merge-branch/scripts/merge.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("wind_hub_merge_skill", MERGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_view(**overrides: Any) -> dict[str, Any]:
    pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "feature/x",
        "baseRefName": "main",
        "headRefOid": "h1",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
        "url": "https://example.test/7",
        "mergedAt": None,
        "mergeCommit": None,
    }
    pr.update(overrides)
    return pr


MERGED_VIEW = {
    "state": "MERGED",
    "mergedAt": "2026-10-18T00:00:00Z",
    "mergeCommit": {"oid": "s1"},
    "baseRefName": "main",
    "url": "https://example.test/7",
}


class FakeHarness:
    """按 merge.py 的调用顺序模拟 git/gh/subprocess。"""

    def __init__(
        self,
        module: Any,
        monkeypatch: pytest.MonkeyPatch,
        *,
        gate_rc: int = 0,
        head_moves: bool = False,
        main_moves: bool = False,
        dirty_after: bool = False,
        merge_error: bool = False,
        state_after_merge_error: str = "MERGED",
        pr_overrides: dict[str, Any] | None = None,
        actions_enabled: bool | None = True,
        actions_error: bool = False,
        classic_checks: dict[str, Any] | None = None,
        rules: list[dict[str, Any]] | None = None,
    ) -> None:
        self.module = module
        self.gate_rc = gate_rc
        self.head_moves = head_moves
        self.main_moves = main_moves
        self.dirty_after = dirty_after
        self.merge_error = merge_error
        self.state_after_merge_error = state_after_merge_error
        self.pr_overrides = pr_overrides or {}
        # bool => actions/permissions.enabled; None => missing/unknown field.
        self.actions_enabled = actions_enabled
        self.actions_error = actions_error
        # None => HTTP 404 (no classic required checks).
        self.classic_checks = classic_checks
        self.rules = rules if rules is not None else []
        self.gate_done = False
        self.merge_calls: list[list[str]] = []
        self.gate_calls = 0
        monkeypatch.setattr(module, "command", self.command)
        monkeypatch.setattr(module, "gh_json", self.gh_json)
        monkeypatch.setattr(module, "gh_api", self.gh_api)
        monkeypatch.setattr(module.subprocess, "run", self.subprocess_run)

    def command(self, *args: str, allow_failure: bool = False) -> str:
        a = list(args)
        if a[:2] == ["git", "branch"]:
            return "feature/x"
        if a[:2] == ["git", "status"]:
            return " M dirty" if self.gate_done and self.dirty_after else ""
        if a[:2] == ["git", "fetch"]:
            return ""
        if a[:2] == ["git", "rev-parse"]:
            target = a[2]
            if target == "HEAD":
                return "h2" if self.gate_done and self.head_moves else "h1"
            if target == "origin/feature/x":
                return "h1"
            if target == "origin/main":
                return "m2" if self.gate_done and self.main_moves else "m1"
        if a[:2] == ["git", "diff"]:
            return "src/core/infrastructure/sink/file.py"
        if a[:3] == ["gh", "pr", "merge"]:
            self.merge_calls.append(a)
            if self.merge_error:
                raise self.module.BlockedError("gh pr merge: network timeout")
            return ""
        raise AssertionError(f"unexpected command: {a}")

    def gh_json(self, *args: str) -> Any:
        a = list(args)
        if a == ["repo", "view", "--json", "nameWithOwner"]:
            return {"nameWithOwner": "o/r"}
        if a == ["pr", "view", "--json", "number"]:
            return {"number": 7}
        fields = a[a.index("--json") + 1] if "--json" in a else ""
        if "statusCheckRollup" in fields:
            return _pr_view(**self.pr_overrides)
        if fields == "headRefOid,baseRefName,state":
            if self.head_moves:
                return {"headRefOid": "h2", "baseRefName": "main", "state": "OPEN"}
            return {"headRefOid": "h1", "baseRefName": "main", "state": "OPEN"}
        if fields == "state":
            return {"state": self.state_after_merge_error}
        if "mergedAt" in fields:
            return MERGED_VIEW
        raise AssertionError(f"unexpected gh json: {a}")

    def gh_api(self, path: str) -> Any:
        if path.endswith("/actions/permissions"):
            if self.actions_error:
                raise self.module.BlockedError("gh api: network failure")
            if self.actions_enabled is None:
                return {}
            return {"enabled": self.actions_enabled}
        if path.endswith("/protection/required_status_checks"):
            return self.classic_checks  # None => 404 absent
        if path.endswith("/rules/branches/main"):
            return self.rules
        raise AssertionError(f"unexpected gh api: {path}")

    def subprocess_run(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if any("ci_gate.py" in str(arg) for arg in args):
            self.gate_done = True
            self.gate_calls += 1
            return subprocess.CompletedProcess(args, self.gate_rc, "", "")
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected subprocess: {args}")


def _run(module: Any, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["merge.py", *argv])
    return module.main()


def test_happy_path_merges_once_and_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch)
    rc = _run(module, monkeypatch, "merge")
    assert rc == 0
    assert harness.gate_calls == 1
    assert harness.merge_calls == [["gh", "pr", "merge", "7", "--squash"]]
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "MERGED"
    assert report["squash_sha"] == "s1"
    assert isinstance(report["local_validation_seconds"], int | float)


def test_check_action_does_not_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch)
    rc = _run(module, monkeypatch, "check")
    assert rc == 0
    assert harness.merge_calls == []
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "READY"
    assert report["head_sha"] == "h1"


def test_gate_failure_blocks_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, gate_rc=1)
    with pytest.raises(module.BlockedError, match="受影响范围验证失败"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_head_change_after_validation_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, head_moves=True)
    with pytest.raises(module.BlockedError, match="HEAD"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_main_change_after_validation_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, main_moves=True)
    with pytest.raises(module.BlockedError, match="main"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_dirty_worktree_after_validation_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, dirty_after=True)
    with pytest.raises(module.BlockedError, match="未提交修改"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_merge_error_then_merged_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    harness = FakeHarness(
        module,
        monkeypatch,
        merge_error=True,
        state_after_merge_error="MERGED",
    )
    rc = _run(module, monkeypatch, "merge")
    assert rc == 0
    assert len(harness.merge_calls) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "MERGED"


def test_merge_error_then_open_reports_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, merge_error=True,
                          state_after_merge_error="OPEN")
    with pytest.raises(module.BlockedError, match="network timeout"):
        _run(module, monkeypatch, "merge")
    assert len(harness.merge_calls) == 1


def test_draft_pr_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(module, monkeypatch, pr_overrides={"isDraft": True})
    with pytest.raises(module.BlockedError, match="Draft"):
        _run(module, monkeypatch, "check")


def test_conflicting_pr_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(
        module,
        monkeypatch,
        pr_overrides={"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"},
    )
    with pytest.raises(module.BlockedError, match="允许合并"):
        _run(module, monkeypatch, "check")


def test_review_changes_requested_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(
        module,
        monkeypatch,
        pr_overrides={"reviewDecision": "CHANGES_REQUESTED"},
    )
    with pytest.raises(module.BlockedError, match="代码审查"):
        _run(module, monkeypatch, "check")


def test_missing_checks_block(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(module, monkeypatch, pr_overrides={"statusCheckRollup": []})
    with pytest.raises(module.BlockedError, match="没有任何 Check"):
        _run(module, monkeypatch, "check")


def test_failed_check_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(
        module,
        monkeypatch,
        pr_overrides={
            "statusCheckRollup": [{"name": "ci", "conclusion": "FAILURE"}]
        },
    )
    with pytest.raises(module.BlockedError, match="CI"):
        _run(module, monkeypatch, "check")


def test_pending_check_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(
        module,
        monkeypatch,
        pr_overrides=[
            {"statusCheckRollup": [{"name": "ci", "conclusion": None}]}
        ][0],
    )
    with pytest.raises(module.BlockedError, match="CI"):
        _run(module, monkeypatch, "check")


def test_wrong_base_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    FakeHarness(module, monkeypatch, pr_overrides={"baseRefName": "release"})
    with pytest.raises(module.BlockedError, match="目标分支"):
        _run(module, monkeypatch, "check")


def test_already_merged_verifies_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    harness = FakeHarness(
        module, monkeypatch, pr_overrides={"state": "MERGED", "mergedAt": "t",
                                          "mergeCommit": {"oid": "s1"}}
    )
    rc = _run(module, monkeypatch, "merge")
    assert rc == 0
    assert harness.merge_calls == []
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "MERGED"


def test_inspect_rejects_dirty_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()

    def command(*args: str, allow_failure: bool = False) -> str:
        if list(args[:2]) == ["git", "branch"]:
            return "feature/x"
        if list(args[:2]) == ["git", "status"]:
            return " M x"
        raise AssertionError(f"unexpected command: {args}")

    def gh_json(*args: str) -> Any:
        raise AssertionError("gh must not be called")

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "gh_json", gh_json)
    with pytest.raises(module.BlockedError, match="不干净"):
        module.inspect(7)


# --- Actions repo-level disabled handling ---------------------------------


def test_actions_disabled_no_required_runs_gate_and_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    harness = FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        pr_overrides={"statusCheckRollup": []},
    )
    rc = _run(module, monkeypatch, "merge")
    assert rc == 0
    assert harness.gate_calls == 1
    assert harness.merge_calls == [["gh", "pr", "merge", "7", "--squash"]]
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "MERGED"
    assert report["ci"] == "NOT_RUN_ACTIONS_DISABLED"


def test_actions_disabled_check_action_annotates_ci(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        pr_overrides={"statusCheckRollup": []},
    )
    rc = _run(module, monkeypatch, "check")
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "READY"
    assert report["ci"] == "NOT_RUN_ACTIONS_DISABLED"


def test_actions_disabled_gate_failure_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        gate_rc=1,
        pr_overrides={"statusCheckRollup": []},
    )
    with pytest.raises(module.BlockedError, match="受影响范围验证失败"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_actions_permission_query_failure_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, actions_error=True)
    with pytest.raises(module.BlockedError, match="network failure"):
        _run(module, monkeypatch, "check")
    assert harness.merge_calls == []


def test_actions_permission_unknown_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(module, monkeypatch, actions_enabled=None)
    with pytest.raises(module.BlockedError, match="enabled 未知"):
        _run(module, monkeypatch, "check")
    assert harness.merge_calls == []


def test_actions_disabled_unmet_ruleset_required_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": "ext/gate", "app_id": 42}]
            },
        }
    ]
    harness = FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        rules=rules,
        pr_overrides={"statusCheckRollup": []},
    )
    with pytest.raises(module.BlockedError, match="外部必需检查"):
        _run(module, monkeypatch, "check")
    assert harness.merge_calls == []


def test_actions_disabled_unmet_classic_required_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    harness = FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        classic_checks={"contexts": ["ext/gate"]},
        pr_overrides={
            "statusCheckRollup": [{"name": "ext/gate", "conclusion": "PENDING"}]
        },
    )
    with pytest.raises(module.BlockedError, match="外部必需检查"):
        _run(module, monkeypatch, "check")
    assert harness.merge_calls == []


def test_actions_disabled_satisfied_external_required_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    harness = FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        classic_checks={"contexts": ["ext/gate"]},
        pr_overrides={
            "statusCheckRollup": [{"context": "ext/gate", "state": "SUCCESS"}]
        },
    )
    rc = _run(module, monkeypatch, "check")
    assert rc == 0
    assert harness.merge_calls == []


def test_actions_disabled_head_change_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(
        module, monkeypatch, actions_enabled=False, head_moves=True
    )
    with pytest.raises(module.BlockedError, match="HEAD"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_actions_disabled_main_change_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(
        module, monkeypatch, actions_enabled=False, main_moves=True
    )
    with pytest.raises(module.BlockedError, match="main"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_actions_disabled_dirty_worktree_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    harness = FakeHarness(
        module, monkeypatch, actions_enabled=False, dirty_after=True
    )
    with pytest.raises(module.BlockedError, match="未提交修改"):
        _run(module, monkeypatch, "merge")
    assert harness.merge_calls == []


def test_actions_disabled_merge_error_then_merged_no_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    harness = FakeHarness(
        module,
        monkeypatch,
        actions_enabled=False,
        merge_error=True,
        state_after_merge_error="MERGED",
    )
    rc = _run(module, monkeypatch, "merge")
    assert rc == 0
    assert len(harness.merge_calls) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "MERGED"
    assert report["ci"] == "NOT_RUN_ACTIONS_DISABLED"
