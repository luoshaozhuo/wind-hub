"""Merged-PR local cleanup is fast-forward-only and fail-closed."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MERGE = ROOT / "ai_shared/agent_config/skills/merge-branch/scripts/merge.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("merge_local_cleanup_test", MERGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("scenario", ["ok", "dirty", "diverged", "occupied"])
def test_local_cleanup(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    module = _load()
    calls: list[tuple[str, ...]] = []
    current = "feature/x"
    head = "h1"

    def command(*args: str, allow_failure: bool = False) -> str:
        nonlocal current, head
        calls.append(args)
        if args == ("git", "branch", "--show-current"):
            return current
        if args == ("git", "status", "--porcelain"):
            return " M file" if scenario == "dirty" else ""
        if args == ("git", "rev-parse", "HEAD"):
            return "s1" if current == "main" else head
        if args == ("git", "rev-parse", "refs/heads/main"):
            return "base"
        if args == ("git", "rev-parse", "origin/main"):
            return "s1"
        if args == ("git", "rev-parse", "refs/heads/feature/x"):
            return "h1"
        if args == ("git", "worktree", "list", "--porcelain"):
            return "branch refs/heads/main" if scenario == "occupied" else ""
        if args == ("git", "switch", "main"):
            current = "main"
        return ""

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(
            args, 1 if scenario == "diverged" and "base" in args else 0, "", ""
        )

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module.subprocess, "run", run)
    pr = {"headRefName": "feature/x", "headRefOid": "h1"}
    if scenario != "ok":
        with pytest.raises(module.BlockedError):
            module.finalize_local(pr, "s1")
        assert ("git", "branch", "-D", "feature/x") not in calls
    else:
        result = module.finalize_local(pr, "s1")
        assert result["local_branch"] == "main"
        assert result["deleted_local_branch"] == "feature/x"
        assert ("git", "merge", "--ff-only", "origin/main") in calls
        assert calls.index(("git", "switch", "main")) < calls.index(
            ("git", "branch", "-D", "feature/x")
        )


def test_cleanup_rejects_wrong_pr_head(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(
        module,
        "command",
        lambda *args, **kwargs: {
            ("git", "branch", "--show-current"): "feature/x",
            ("git", "status", "--porcelain"): "",
            ("git", "rev-parse", "HEAD"): "unexpected",
        }[args],
    )
    with pytest.raises(module.BlockedError, match="HEAD"):
        module.finalize_local({"headRefName": "feature/x", "headRefOid": "h1"}, "s1")
