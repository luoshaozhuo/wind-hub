"""Unit tests for the ``wind-hub`` CLI — commands are exercised against mock services."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from typer.testing import CliRunner

from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.context import AppContext, clear_context, set_context
from wind_hub.domain.model.command import CommandResult
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.model.route import RouteDecision
from wind_hub.domain.port.inbound import SystemStatus


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_context()
    yield
    clear_context()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _context(
    *,
    command: AsyncMock | None = None,
    task: AsyncMock | None = None,
    query: AsyncMock | None = None,
    router: MagicMock | None = None,
) -> AppContext:
    return AppContext(
        command_service=command or AsyncMock(),
        task_service=task or AsyncMock(),
        query_service=query or AsyncMock(),
        router=router,
    )


# ---------------------------------------------------------------------------
# build_cli
# ---------------------------------------------------------------------------


def test_build_cli_returns_typer() -> None:
    assert isinstance(build_cli(), typer.Typer)


def test_all_subcommands_registered(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["--help"])
    assert result.exit_code == 0
    for name in ["run", "validate", "reload", "status", "devices", "point", "cmd", "route"]:
        assert name in result.output


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _write_valid_config(base: str) -> None:
    from pathlib import Path

    (Path(base) / "system.yaml").write_text("{}\n", encoding="utf-8")
    (Path(base) / "devices.yaml").write_text("devices: []\n", encoding="utf-8")
    (Path(base) / "points.yaml").write_text("points: []\n", encoding="utf-8")
    (Path(base) / "routing.yaml").write_text("rules: []\n", encoding="utf-8")


def test_validate_success_exit_zero(runner: CliRunner, tmp_path) -> None:  # noqa: ANN001
    _write_valid_config(str(tmp_path))
    result = runner.invoke(build_cli(), ["validate", "--config", str(tmp_path)])
    assert result.exit_code == 0
    assert "配置有效" in result.output


def test_validate_failure_exit_one(runner: CliRunner, tmp_path) -> None:  # noqa: ANN001
    result = runner.invoke(build_cli(), ["validate", "--config", str(tmp_path)])
    assert result.exit_code == 1
    assert "配置校验失败" in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_table(runner: CliRunner) -> None:
    task = AsyncMock()
    task.status.return_value = SystemStatus(running=True, device_count=2, sink_count=1)
    set_context(_context(task=task))

    result = runner.invoke(build_cli(), ["status"])
    assert result.exit_code == 0
    assert "device_count" in result.output
    assert "sink_count" in result.output


def test_status_json(runner: CliRunner) -> None:
    task = AsyncMock()
    task.status.return_value = SystemStatus(running=True, device_count=2, sink_count=1)
    set_context(_context(task=task))

    result = runner.invoke(build_cli(), ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["running"] is True
    assert payload["device_count"] == 2


def test_status_without_context_exits_one(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["status"])
    assert result.exit_code == 1
    assert "AppContext" in result.output


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


def test_devices_lists_json(runner: CliRunner) -> None:
    query = AsyncMock()
    query.list_devices.return_value = [
        DeviceInfo(device_id="d1", protocol="modbus", connected=True),
    ]
    set_context(_context(query=query))

    result = runner.invoke(build_cli(), ["devices", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["device_id"] == "d1"


# ---------------------------------------------------------------------------
# point read
# ---------------------------------------------------------------------------


def test_point_read_calls_query_service(runner: CliRunner) -> None:
    query = AsyncMock()
    query.read_point.return_value = PointValue(
        device_id="d1", point_id="rotor.speed", value=1500.5, quality=Quality.GOOD, source="modbus"
    )
    set_context(_context(query=query))

    result = runner.invoke(build_cli(), ["point", "read", "d1", "rotor.speed"])
    assert result.exit_code == 0
    query.read_point.assert_awaited_once_with("d1", "rotor.speed")
    assert "1500.5" in result.output


# ---------------------------------------------------------------------------
# cmd send
# ---------------------------------------------------------------------------


def test_cmd_send_calls_command_service(runner: CliRunner) -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="c1", success=True)
    set_context(_context(command=command))

    result = runner.invoke(build_cli(), ["cmd", "send", "d1", "setpoint", "42.5"])
    assert result.exit_code == 0
    assert command.send.await_count == 1
    sent = command.send.call_args.args[0]
    assert sent.device_id == "d1"
    assert sent.point_id == "setpoint"
    assert sent.value == 42.5  # coerced from string to float


def test_cmd_send_bool_coercion(runner: CliRunner) -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="c1", success=True)
    set_context(_context(command=command))

    result = runner.invoke(build_cli(), ["cmd", "send", "d1", "switch", "true"])
    assert result.exit_code == 0
    assert command.send.call_args.args[0].value is True


def test_cmd_send_failure_exits_one(runner: CliRunner) -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="c1", success=False, error="boom")
    set_context(_context(command=command))

    result = runner.invoke(build_cli(), ["cmd", "send", "d1", "p", "1"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# route explain
# ---------------------------------------------------------------------------


def test_route_explain_calls_router(runner: CliRunner) -> None:
    router = MagicMock()
    router.explain.return_value = RouteDecision(
        device_id="d1",
        point_id="rotor.speed",
        targets=["s1"],
        matched_rule="default",
        source="rule",
    )
    set_context(_context(router=router))

    result = runner.invoke(build_cli(), ["route", "explain", "d1", "rotor.speed"])
    assert result.exit_code == 0
    router.explain.assert_called_once_with("d1", "rotor.speed")
    assert "s1" in result.output


def test_route_explain_without_router_exits_one(runner: CliRunner) -> None:
    set_context(_context())
    result = runner.invoke(build_cli(), ["route", "explain", "d1", "p1"])
    assert result.exit_code == 1
