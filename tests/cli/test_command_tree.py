"""Typer 命令树、单次调用、JSON 信封与退出码（设计 UC-01/PR-01～05）。"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import _command_words, _dispatch_typer

runner = CliRunner()


def test_command_tree_covers_design_leaf_commands() -> None:
    words = set(_command_words())
    expected = {
        "service list",
        "service start",
        "service stop",
        "service restart",
        "service status",
        "service info",
        "service capabilities",
        "config diff",
        "config get",
        "config push",
        "config apply",
        "net ping",
        "net tcp",
        "net industrial",
        "net scan",
        "collector devices",
        "collector device",
        "collector points",
        "collector point",
        "collector tasks",
        "collector task",
        "collector start",
        "collector stop",
        "collector latest",
        "collector watch",
        "collector publishing",
        "collector metrics",
        "collector events",
        "commander devices",
        "commander device",
        "commander points",
        "commander write",
        "commander result",
        "commander metrics",
        "commander events",
        "sink deliveries",
        "sink delivery",
        "sink start",
        "sink stop",
        "sink metrics",
        "sink events",
        "gateway list",
        "gateway status",
        "gateway variables",
        "gateway read",
        "gateway start",
        "gateway stop",
        "gateway metrics",
        "gateway events",
        "data-generator devices",
        "data-generator device",
        "data-generator scenarios",
        "data-generator start",
        "data-generator stop",
        "data-generator status",
        "data-generator metrics",
        "data-generator events",
        "diagnose collector device",
        "diagnose collector pipeline",
        "diagnose collector gi",
        "diagnose commander",
        "diagnose sink",
        "diagnose gateway",
        "diagnose data-generator",
        "diagnose pipeline",
        "help",
        "exit",
    }
    missing = expected - words
    assert not missing, f"设计命令缺失: {sorted(missing)}"


def test_help_lists_all_leaf_commands() -> None:
    result = runner.invoke(cli_main.app, ["help"])
    assert result.exit_code == 0
    assert "collector devices" in result.stdout
    assert "commander write" in result.stdout


def test_unknown_command_exit_code_2() -> None:
    result = runner.invoke(cli_main.app, ["frobnicate"])
    assert result.exit_code == 2


def test_shell_dispatch_unknown_command_raises_valueerror() -> None:
    # Shell 路径曾因 Typer vendored click 类型不一致而逃逸异常（已修复的 P1）。
    with pytest.raises(ValueError, match="No such command"):
        _dispatch_typer(["frobnicate"])


def test_invalid_format_option_exit_2() -> None:
    result = runner.invoke(cli_main.app, ["--format", "yaml", "help"])
    assert result.exit_code == 2


def test_remote_command_without_config_is_exit_2() -> None:
    result = runner.invoke(cli_main.app, ["--format", "json", "service", "list"])
    assert result.exit_code == 2
    envelope = json.loads(result.stdout.strip().splitlines()[-1])
    assert envelope["status"] == "FAILURE"
    assert envelope["error_code"] == "ValueError"


def test_net_tcp_closed_json_envelope_exit_3() -> None:
    result = runner.invoke(
        cli_main.app, ["--format", "json", "net", "tcp", "127.0.0.1", "1", "--timeout", "0.2"]
    )
    envelope = json.loads(result.stdout.strip())
    assert envelope["schema_version"] == 1
    assert envelope["command"] == "net tcp"
    assert envelope["status"] == "UNKNOWN"
    assert envelope["data"]["tcp_state"] in {"CLOSED", "TIMEOUT"}
    assert result.exit_code == 3


def test_net_invalid_argument_exit_2() -> None:
    result = runner.invoke(cli_main.app, ["net", "tcp", "not-an-ip", "1"])
    assert result.exit_code == 2
