"""ctl 查询类命令 functional 测试——info / status / devices / read / tasks。

全链路断言：CLI 参数 → gRPC → 真实 Runtime → 真实 Modbus fixture，
输出为 ctl 打印的 JSON 载荷。查询命令本身不改变被控侧状态，
可独立运行于共享模块环境的任意时刻。
"""

from __future__ import annotations


import pytest

pytestmark = pytest.mark.modbus

from tests.ctl.functional.conftest import CtlCliResult


class TestInfoAndStatus:
    def test_info_reports_collector_identity(self, run_ctl) -> None:
        result = run_ctl("info")
        assert result.returncode == 0, result.stderr
        assert result.payload["collector_id"] == "ctl-functional"
        assert result.payload["runtime_running"] is True
        assert result.payload["boot_config_hash"] == "hash-ctl-functional"

    def test_status_reports_runtime_aggregation(self, run_ctl) -> None:
        result = run_ctl("status")
        assert result.returncode == 0, result.stderr
        assert result.payload["running"] is True
        assert result.payload["device_count"] == 1
        assert result.payload["sink_count"] == 1
        assert result.payload["devices_connected"] == 1

    def test_unreachable_target_exits_2(self, run_ctl) -> None:
        from tests.system.process import free_port

        result = run_ctl(
            "status", target=f"127.0.0.1:{free_port()}", rpc_timeout=1.0
        )
        assert result.returncode == 2
        assert result.payload is None
        assert "RPC failed" in result.stderr

    def test_devices_lists_modbus_device(self, run_ctl) -> None:
        result = run_ctl("devices")
        assert result.returncode == 0, result.stderr
        items = result.payload["items"]
        assert [item["device_id"] for item in items] == ["modbus-1"]
        assert items[0]["protocol"] == "modbus"
        assert items[0]["connected"] is True


class TestReadCommand:
    def test_read_point_returns_protocol_value(self, run_ctl) -> None:
        result = run_ctl("read", "modbus-1", "rotor.speed")
        assert result.returncode == 0, result.stderr
        assert result.payload["device_id"] == "modbus-1"
        assert result.payload["point_id"] == "rotor.speed"
        assert result.payload["value"] == pytest.approx(1200.5)

    def test_read_unknown_device_exits_2(self, run_ctl) -> None:
        result = run_ctl("read", "ghost", "rotor.speed")
        assert result.returncode == 2
        assert result.payload is None
        assert "RPC failed" in result.stderr

    def test_read_unknown_point_exits_2(self, run_ctl) -> None:
        result = run_ctl("read", "modbus-1", "ghost.point")
        assert result.returncode == 2
        assert "RPC failed" in result.stderr


class TestTaskQueries:
    def test_tasks_lists_definition_with_summary(self, run_ctl) -> None:
        result = run_ctl("tasks")
        assert result.returncode == 0, result.stderr
        items = result.payload["items"]
        assert [item["task_id"] for item in items] == ["modbus-telemetry"]
        assert items[0]["point_group"] == "telemetry"
        assert items[0]["runtime_state"] in ("running", "stopped")

    def test_task_returns_single_summary(self, run_ctl) -> None:
        result = run_ctl("task", "modbus-telemetry")
        assert result.returncode == 0, result.stderr
        assert result.payload["task_id"] == "modbus-telemetry"
        assert result.payload["instance_count"] == 1

    def test_task_unknown_exits_2(self, run_ctl) -> None:
        result = run_ctl("task", "ghost-task")
        assert result.returncode == 2
        assert "RPC failed" in result.stderr

    def test_task_instances_lists_expanded_instances(self, run_ctl) -> None:
        result = run_ctl("task-instances")
        assert result.returncode == 0, result.stderr
        items = result.payload["items"]
        assert [item["instance_id"] for item in items] == [
            "modbus-telemetry:modbus-1"
        ]
        assert items[0]["state"] in ("running", "stopped")
