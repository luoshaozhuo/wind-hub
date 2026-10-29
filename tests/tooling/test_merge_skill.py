"""Direct squash-merge skill safety contract tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit
SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ai_shared/agent_config/skills/merge-branch/scripts/merge.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("wind_hub_merge_skill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_squash_without_pr_or_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    commands: list[tuple[str, ...]] = []
    values = {
        ("git", "branch", "--show-current"): "feat/example",
        ("git", "status", "--porcelain"): "",
        ("git", "rev-parse", "HEAD"): "head",
        ("git", "rev-parse", "refs/remotes/origin/feat/example"): "head",
        ("git", "rev-parse", "refs/remotes/origin/main"): "squash",
        ("git", "rev-parse", "head^{tree}"): "tree",
        ("git", "commit-tree", "tree", "-p", "base", "-m",
         "feat: squash merge feat/example into main"): "squash",
        ("git", "worktree", "list", "--porcelain"): "",
        ("git", "rev-parse", "refs/heads/main"): "base",
    }
    fetched = False

    def fake_run(*args: str) -> str:
        nonlocal fetched
        commands.append(args)
        if args == ("git", "fetch", "origin", "main", "feat/example"):
            return ""
        if args == ("git", "fetch", "origin", "main"):
            fetched = True
            return ""
        if args == ("git", "rev-parse", "refs/remotes/origin/main"):
            return "squash" if fetched else "base"
        if args in {
            ("git", "push", "origin", "squash:refs/heads/main"),
            ("git", "switch", "main"),
            ("git", "merge", "--ff-only", "origin/main"),
            ("git", "branch", "-D", "feat/example"),
            ("git", "fetch", "origin", "feat/example"),
            ("git", "push", "origin", "--delete", "feat/example"),
        }:
            return ""
        return values[args]

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "is_ancestor", lambda _a, _b: True)
    result = module.merge()
    assert result["state"] == "MERGED_AND_CLEANED"
    assert ("git", "push", "origin", "squash:refs/heads/main") in commands
    assert all("gh" not in command and "ci_gate.py" not in command for command in commands)


def test_dirty_worktree_blocks_before_push(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(
        module,
        "run",
        lambda *args: "feat/example" if args[1] == "branch" else " M file",
    )
    with pytest.raises(module.BlockedError, match="不干净"):
        module.merge()


def test_server_rejection_preserves_source_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    commands: list[tuple[str, ...]] = []
    values = {
        ("git", "branch", "--show-current"): "feat/example",
        ("git", "status", "--porcelain"): "",
        ("git", "rev-parse", "HEAD"): "head",
        ("git", "rev-parse", "refs/remotes/origin/feat/example"): "head",
        ("git", "rev-parse", "refs/remotes/origin/main"): "base",
        ("git", "rev-parse", "head^{tree}"): "tree",
        ("git", "commit-tree", "tree", "-p", "base", "-m",
         "feat: squash merge feat/example into main"): "squash",
    }

    def fake_run(*args: str) -> str:
        commands.append(args)
        if args == ("git", "push", "origin", "squash:refs/heads/main"):
            raise module.BlockedError("branch protection requires pull request")
        if args[:2] == ("git", "fetch"):
            return ""
        return values[args]

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "is_ancestor", lambda _a, _b: True)
    with pytest.raises(module.BlockedError, match="branch protection"):
        module.merge()
    assert ("git", "push", "origin", "--delete", "feat/example") not in commands
