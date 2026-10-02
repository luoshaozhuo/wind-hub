"""ctl Task 控制命令 functional 测试。

共享模块环境：每个测试先显式建立前置状态（start/stop 均幂等），
不依赖其他测试的执行顺序。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.modbus


from tests.ctl.functional.conftest import INSTANCE_ID


class TestInstanceCommands:
    def test_start_instance(self, run_ctl) -> None:
        # 前置：确保实例处于 STOPPED。
        run_ctl("stop-instance", INSTANCE_ID)
        result = run_ctl("start-instance", INSTANCE_ID)
        assert result.returncode == 0, result.stderr
        assert result.payload["instance_id"] == INSTANCE_ID
        assert result.payload["state"] == "running"

    def test_start_instance_is_idempotent(self, run_ctl) -> None:
        run_ctl("start-instance", INSTANCE_ID)
        result = run_ctl("start-instance", INSTANCE_ID)
        assert result.returncode == 0, result.stderr
        assert result.payload["state"] == "running"

    def test_stop_instance(self, run_ctl) -> None:
        run_ctl("start-instance", INSTANCE_ID)
        result = run_ctl("stop-instance", INSTANCE_ID)
        assert result.returncode == 0, result.stderr
        assert result.payload["state"] == "stopped"

    def test_stop_instance_is_idempotent(self, run_ctl) -> None:
        run_ctl("stop-instance", INSTANCE_ID)
        result = run_ctl("stop-instance", INSTANCE_ID)
        assert result.returncode == 0, result.stderr
        assert result.payload["state"] == "stopped"

    def test_unknown_instance_exits_2(self, run_ctl) -> None:
        result = run_ctl("start-instance", "modbus-telemetry:ghost")
        assert result.returncode == 2
        assert "RPC failed" in result.stderr


class TestTaskCommands:
    def test_start_task(self, run_ctl) -> None:
        run_ctl("stop", "modbus-telemetry")
        result = run_ctl("start", "modbus-telemetry")
        assert result.returncode == 0, result.stderr
        assert result.payload["task_id"] == "modbus-telemetry"
        assert result.payload["runtime_state"] == "running"

    def test_stop_task(self, run_ctl) -> None:
        run_ctl("start", "modbus-telemetry")
        result = run_ctl("stop", "modbus-telemetry")
        assert result.returncode == 0, result.stderr
        assert result.payload["runtime_state"] == "stopped"

    def test_unknown_task_exits_2(self, run_ctl) -> None:
        for command in ("start", "stop"):
            result = run_ctl(command, "ghost-task")
            assert result.returncode == 2
            assert "RPC failed" in result.stderr


class TestBatchCommands:
    def test_start_all_reports_changed_counts(self, run_ctl) -> None:
        run_ctl("stop-all")
        result = run_ctl("start-all")
        assert result.returncode == 0, result.stderr
        assert result.payload["total"] == 1
        assert result.payload["changed"] == 1
        assert result.payload["unchanged"] == 0

    def test_stop_all_reports_changed_counts(self, run_ctl) -> None:
        run_ctl("start-all")
        result = run_ctl("stop-all")
        assert result.returncode == 0, result.stderr
        assert result.payload["total"] == 1
        assert result.payload["changed"] == 1

    def test_repeated_batch_counts_unchanged(self, run_ctl) -> None:
        run_ctl("start-all")
        result = run_ctl("start-all")
        assert result.returncode == 0, result.stderr
        assert result.payload["changed"] == 0
        assert result.payload["unchanged"] == 1

    def test_task_state_visible_via_query_after_control(self, run_ctl) -> None:
        run_ctl("stop-all")
        result = run_ctl("task-instance", INSTANCE_ID)
        assert result.returncode == 0, result.stderr
        assert result.payload["state"] == "stopped"

        run_ctl("start-all")
        result = run_ctl("task-instance", INSTANCE_ID)
        assert result.payload["state"] == "running"
