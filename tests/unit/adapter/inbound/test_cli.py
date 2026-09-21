"""Unit tests for the ``wind-hub`` CLI — commands are exercised against mock services."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import typer
from typer.testing import CliRunner

from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.application.app_context import AppContext, clear_context, set_context
from wind_hub.application.runtime.task_instance import TaskInstanceState
from wind_hub.application.usecase import (
    SystemStatus,
    TaskBatchResult,
    TaskDetail,
    TaskInstanceDetail,
)
from wind_hub.domain.model.command import CommandResult
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.point import PointValue, Quality


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
    config: AsyncMock | None = None,
    tasks: AsyncMock | None = None,
    runtime: AsyncMock | None = None,
) -> AppContext:
    """AppContext 新模型：command/query/config/tasks/runtime（无 route_query/jobs）。"""
    return AppContext(
        command=command or AsyncMock(),
        query=query or AsyncMock(),
        config=config,
        tasks=tasks,
        runtime=runtime,
    )


# ---------------------------------------------------------------------------
# build_cli
# ---------------------------------------------------------------------------


def test_build_cli_returns_typer() -> None:
    assert isinstance(build_cli(), typer.Typer)


def test_all_subcommands_registered(runner: CliRunner) -> None:
    result = runner.invoke(build_cli(), ["--help"])
    assert result.exit_code == 0
    for name in ["run", "validate", "reload", "status", "devices", "tasks", "point", "cmd"]:
        assert name in result.output


def test_no_jobs_or_route_subcommands(runner: CliRunner) -> None:
    """旧模型命令（jobs / route）已随重构删除。"""
    result = runner.invoke(build_cli(), ["--help"])
    assert result.exit_code == 0
    assert "jobs" not in result.output
    assert "route" not in result.output

    assert runner.invoke(build_cli(), ["jobs"]).exit_code != 0
    assert runner.invoke(build_cli(), ["route"]).exit_code != 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _write_valid_config(base: Path) -> None:
    (base / "system.yaml").write_text("{}\n", encoding="utf-8")
    (base / "devices.yaml").write_text("devices: []\n", encoding="utf-8")
    (base / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
    (base / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")


def test_validate_success_exit_zero(runner: CliRunner, tmp_path: Path) -> None:
    _write_valid_config(tmp_path)
    result = runner.invoke(build_cli(), ["validate", "--config", str(tmp_path)])
    assert result.exit_code == 0
    assert "配置有效" in result.output
    assert "0 个采集任务" in result.output


def test_validate_reports_task_count(runner: CliRunner, tmp_path: Path) -> None:
    """validate 输出包含「N 个采集任务」（tasks.yaml 取代 routing.yaml）。"""
    (tmp_path / "system.yaml").write_text(
        "sinks:\n  - {name: archive, type: file, params: {path: /tmp/x.csv}}\n",
        encoding="utf-8",
    )
    (tmp_path / "devices.yaml").write_text(
        "devices:\n"
        "  - device_id: d1\n"
        "    protocol: modbus\n"
        "    point_table: wtg\n"
        "    endpoint: {host: 10.0.0.1, port: 502}\n",
        encoding="utf-8",
    )
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n"
        "  wtg:\n"
        "    points:\n"
        "      - point_id: p1\n"
        "        point_groups: [fast]\n"
        "        address: {register_type: holding, address: 100}\n"
        "        data_type: float32\n",
        encoding="utf-8",
    )
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n"
        "  - task_id: fast\n"
        "    device: d1\n"
        "    point_group: fast\n"
        "    interval: 1.0\n"
        "    targets: [{sink: archive}]\n",
        encoding="utf-8",
    )
    result = runner.invoke(build_cli(), ["validate", "--config", str(tmp_path)])
    assert result.exit_code == 0
    assert "1 个采集任务" in result.output


def test_validate_failure_exit_one(runner: CliRunner, tmp_path: Path) -> None:
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
# tasks —— 采集 Task / Task Instance 生命周期
# ---------------------------------------------------------------------------

_INSTANCE_ID = "fast:d1"


def _task_detail(task_id: str = "fast") -> TaskDetail:
    return TaskDetail(
        task_id=task_id,
        device="d1",
        device_group=None,
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        enabled=True,
    )


def _instance_detail(
    instance_id: str = _INSTANCE_ID, state: TaskInstanceState = TaskInstanceState.STOPPED
) -> TaskInstanceDetail:
    return TaskInstanceDetail(
        instance_id=instance_id,
        task_id="fast",
        device_id="d1",
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        state=state,
    )


def test_tasks_list_table(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.list_tasks.return_value = [_task_detail()]
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "list"])

    assert result.exit_code == 0
    assert "fast" in result.output
    assert "d1" in result.output
    assert "archive" in result.output
    tasks.list_tasks.assert_awaited_once()


def test_tasks_list_json(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.list_tasks.return_value = [_task_detail()]
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["task_id"] == "fast"
    assert payload[0]["device"] == "d1"
    assert payload[0]["point_group"] == "fast"
    assert payload[0]["targets"] == ["archive"]
    assert payload[0]["enabled"] is True


def test_tasks_instances_table(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.list_instances.return_value = [_instance_detail()]
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "instances"])

    assert result.exit_code == 0
    assert _INSTANCE_ID in result.output
    assert "stopped" in result.output
    tasks.list_instances.assert_awaited_once()


def test_tasks_instances_json(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.list_instances.return_value = [_instance_detail(state=TaskInstanceState.RUNNING)]
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "instances", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["instance_id"] == _INSTANCE_ID
    assert payload[0]["state"] == "running"


def test_tasks_show_instance(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.get_instance.return_value = _instance_detail()
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "show", _INSTANCE_ID])

    assert result.exit_code == 0
    assert _INSTANCE_ID in result.output
    tasks.get_instance.assert_awaited_once_with(_INSTANCE_ID)


def test_tasks_show_json(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.get_instance.return_value = _instance_detail()
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "show", _INSTANCE_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["instance_id"] == _INSTANCE_ID
    assert payload["state"] == "stopped"


def test_tasks_show_unknown_exits_one(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.get_instance.side_effect = KeyError("nope:d1")
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "show", "nope:d1"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_tasks_start_calls_service(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.start_instance.return_value = _instance_detail(state=TaskInstanceState.RUNNING)
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "start", _INSTANCE_ID])

    assert result.exit_code == 0
    tasks.start_instance.assert_awaited_once_with(_INSTANCE_ID)
    assert "running" in result.output


def test_tasks_start_json(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.start_instance.return_value = _instance_detail(state=TaskInstanceState.RUNNING)
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "start", _INSTANCE_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["state"] == "running"


def test_tasks_start_unknown_exits_one(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.start_instance.side_effect = KeyError("nope:d1")
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "start", "nope:d1"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_tasks_stop_calls_service(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.stop_instance.return_value = _instance_detail()
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "stop", _INSTANCE_ID])

    assert result.exit_code == 0
    tasks.stop_instance.assert_awaited_once_with(_INSTANCE_ID)
    assert "stopped" in result.output


def test_tasks_stop_unknown_exits_one(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.stop_instance.side_effect = KeyError("nope:d1")
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "stop", "nope:d1"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_tasks_start_all_reports_summary(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.start_all_instances.return_value = TaskBatchResult(total=3, changed=2, unchanged=1)
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "start-all"])

    assert result.exit_code == 0
    tasks.start_all_instances.assert_awaited_once()
    assert "2" in result.output


def test_tasks_start_all_json(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.start_all_instances.return_value = TaskBatchResult(total=3, changed=2, unchanged=1)
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "start-all", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"total": 3, "changed": 2, "unchanged": 1}


def test_tasks_stop_all_reports_summary(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.stop_all_instances.return_value = TaskBatchResult(total=3, changed=3, unchanged=0)
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "stop-all"])

    assert result.exit_code == 0
    tasks.stop_all_instances.assert_awaited_once()
    assert "3" in result.output


def test_tasks_stop_all_json(runner: CliRunner) -> None:
    tasks = AsyncMock()
    tasks.stop_all_instances.return_value = TaskBatchResult(total=3, changed=3, unchanged=0)
    set_context(_context(tasks=tasks))

    result = runner.invoke(build_cli(), ["tasks", "stop-all", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"total": 3, "changed": 3, "unchanged": 0}


def test_tasks_without_usecase_exits_one(runner: CliRunner) -> None:
    set_context(_context())

    result = runner.invoke(build_cli(), ["tasks", "list"])

    assert result.exit_code == 1
    assert "Task" in result.output
