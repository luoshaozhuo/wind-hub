"""Temporary diagnostic: print Ruff's exact import sorting diff."""

import subprocess

import pytest


def test_show_ruff_import_diff() -> None:
    result = subprocess.run(
        [
            "ruff",
            "check",
            "--select",
            "I",
            "--fix",
            "--diff",
            "src/collector/application/sink_port.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    pytest.fail(f"ruff diff:\n{result.stdout}\n{result.stderr}")
