"""git-safety-gate 危险命令拦截回归测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "ai_shared/agent_config/hooks/git-safety-gate.py"


def _decision(command: str) -> dict[str, str]:
    payload = json.dumps({"tool_input": {"command": command}})
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        return {"decision": "allow"}
    data = json.loads(result.stdout)
    return data["hookSpecificOutput"]


def _denied(command: str) -> bool:
    return _decision(command).get("permissionDecision") == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git merge main",
        "git merge --abort",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git rebase -i HEAD~2",
        "git tag v1.0.0",
        "gh pr merge 90 --squash",
        "gh pr close 90",
        "git push origin main",
        "git push --force origin feature/x",
    ],
)
def test_dangerous_commands_denied(command: str) -> None:
    assert _denied(command)


@pytest.mark.parametrize(
    "command",
    [
        "git merge-base --is-ancestor a b",
        "git merge-tree --write-tree a b",
        "git status",
        "git fetch origin",
        "git log -1 --oneline",
        "git rev-parse HEAD",
        "gh pr view 90",
        "gh run view 123",
    ],
)
def test_readonly_commands_allowed(command: str) -> None:
    assert not _denied(command)
