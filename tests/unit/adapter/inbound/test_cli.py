"""Unit tests for the ``wind-hub`` CLI — commands are exercised against mock services."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from typer.testing import CliRunner

from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.application.app_context import AppContext, clear_context, set_context
from wind_hub.application.port.scheduling import JobState
from wind_hub.application.usecase import JobBatchResult, JobDetail, SystemStatus
from wind_hub.domain.model.command import CommandResult
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.model.route import RouteDecision


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
    query: AsyncMock | None = None,
    router: MagicMock | None = None,
) -> AppContext:
    return AppContext(
        command=command or AsyncMock(),
        query=query or AsyncMock(),
        route_query=router,
    )


# ---------------------------------------------------------------------------
# build_cli
# ---------------------------------------------------------------------------


def test_build_cli_returns_typer() -> None:
    assert isinstance(build_cli(), typer.Typer)


def test_all_subcommands_registered(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["--help"])
    assert result.exit_code == 0
    for name in ["run", "validate", "reload", "status", "devices", "jobs", "point", "cmd", "route"]:
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
    query = AsyncMock()
    query.status.return_value = SystemStatus(running=True, device_count=2, sink_count=1)
    set_context(_context(query=query))

    result = runner.invoke(build_cli(), ["status"])
    assert result.exit_code == 0
    assert "device_count" in result.output
    assert "sink_count" in result.output


def test_status_json(runner: CliRunner) -> None:
    query = AsyncMock()
    query.status.return_value = SystemStatus(running=True, device_count=2, sink_count=1)
    set_context(_context(query=query))

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


def test_point_read_calls_query_usecase(runner: CliRunner) -> None:
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


def test_cmd_send_calls_command_usecase(runner: CliRunner) -> None:
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


# ---------------------------------------------------------------------------
# jobs —— 采集 Job 生命周期
# ---------------------------------------------------------------------------


def _job_detail(job_id: str, state: str = "stopped") -> JobDetail:
    return JobDetail(
        job_id=job_id,
        device_id="d1",
        group="fast",
        interval_seconds=1.0,
        state=JobState(state),
        next_run_time=None,
    )


def _jobs_context(jobs: AsyncMock) -> AppContext:
    return AppContext(
        command=AsyncMock(),
        query=AsyncMock(),
        jobs=jobs,
    )


def test_jobs_list_table(runner: CliRunner) -> None:
    jobs = AsyncMock()
    jobs.list_jobs.return_value = [_job_detail("poll:d1:fast")]
    set_context(_jobs_context(jobs))

    result = runner.invoke(build_cli(), ["jobs", "list"])

    assert result.exit_code == 0
    assert "poll:d1:fast" in result.output
    assert "d1" in result.output
    assert "fast" in result.output
    assert "stopped" in result.output
    jobs.list_jobs.assert_awaited_once()


def test_jobs_show_unknown_exits_one(runner: CliRunner) -> None:
    jobs = AsyncMock()
    jobs.get_job.side_effect = KeyError("poll:nope:fast")
    set_context(_jobs_context(jobs))

    result = runner.invoke(build_cli(), ["jobs", "show", "poll:nope:fast"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_jobs_start_calls_service(runner: CliRunner) -> None:
    jobs = AsyncMock()
    jobs.start_job.return_value = _job_detail("poll:d1:fast", state="running")
    set_context(_jobs_context(jobs))

    result = runner.invoke(build_cli(), ["jobs", "start", "poll:d1:fast"])

    assert result.exit_code == 0
    jobs.start_job.assert_awaited_once_with("poll:d1:fast")
    assert "running" in result.output


def test_jobs_stop_calls_service(runner: CliRunner) -> None:
    jobs = AsyncMock()
    jobs.stop_job.return_value = _job_detail("poll:d1:fast")
    set_context(_jobs_context(jobs))

    result = runner.invoke(build_cli(), ["jobs", "stop", "poll:d1:fast"])

    assert result.exit_code == 0
    jobs.stop_job.assert_awaited_once_with("poll:d1:fast")


def test_jobs_start_all_reports_summary(runner: CliRunner) -> None:
    jobs = AsyncMock()
    jobs.start_all_jobs.return_value = JobBatchResult(total=3, changed=2, unchanged=1)
    set_context(_jobs_context(jobs))

    result = runner.invoke(build_cli(), ["jobs", "start-all"])

    assert result.exit_code == 0
    jobs.start_all_jobs.assert_awaited_once()
    assert "2" in result.output


def test_jobs_stop_all_reports_summary(runner: CliRunner) -> None:
    jobs = AsyncMock()
    jobs.stop_all_jobs.return_value = JobBatchResult(total=3, changed=3, unchanged=0)
    set_context(_jobs_context(jobs))

    result = runner.invoke(build_cli(), ["jobs", "stop-all"])

    assert result.exit_code == 0
    jobs.stop_all_jobs.assert_awaited_once()
    assert "3" in result.output


def test_jobs_without_usecase_exits_one(runner: CliRunner) -> None:
    set_context(AppContext(command=AsyncMock(), query=AsyncMock()))

    result = runner.invoke(build_cli(), ["jobs", "list"])

    assert result.exit_code == 1
    assert "jobs" in result.output
