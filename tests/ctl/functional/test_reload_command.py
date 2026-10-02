"""ctl reload 命令 functional 测试——经 gRPC 触发真实配置热重载。

直接改写模块共享环境的配置目录；每个用例结束后恢复原始配置并
再次 reload，避免污染同模块其他测试。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.modbus


from typing import Any

from tests.collector.functional.conftest import update_yaml
from tests.ctl.functional.conftest import CtlEnvironment

TASKS = "tasks.yaml"


class TestReloadCommand:
    def test_reload_applies_task_change(self, run_ctl, ctl_env: CtlEnvironment) -> None:
        original = 0.2

        def mutate(data: dict[str, Any]) -> None:
            data["tasks"][0]["interval"] = 0.9

        def restore(data: dict[str, Any]) -> None:
            data["tasks"][0]["interval"] = original

        update_yaml(ctl_env.config_dir, TASKS, mutate)
        try:
            result = run_ctl("reload")
            assert result.returncode == 0, result.stderr
            assert result.payload["success"] is True

            task = run_ctl("task", "modbus-telemetry")
            assert task.payload["interval"] == 0.9
        finally:
            update_yaml(ctl_env.config_dir, TASKS, restore)
            run_ctl("reload")

    def test_reload_failure_returns_errors_without_touching_runtime(
        self, run_ctl, ctl_env: CtlEnvironment
    ) -> None:
        def break_it(data: dict[str, Any]) -> None:
            data["tasks"][0]["targets"] = [{"sink": "ghost_sink"}]

        def restore(data: dict[str, Any]) -> None:
            data["tasks"][0]["targets"] = [{"sink": "null_sink"}]

        update_yaml(ctl_env.config_dir, TASKS, break_it)
        try:
            result = run_ctl("reload")
            # 加载失败是业务结果而非 RPC 失败：退出码 0，success=False。
            assert result.returncode == 0, result.stderr
            assert result.payload["success"] is False
            assert result.payload["errors"]

            # 运行态保持原配置。
            task = run_ctl("task", "modbus-telemetry")
            assert task.payload["targets"] == ["null_sink"]
        finally:
            update_yaml(ctl_env.config_dir, TASKS, restore)
            recovered = run_ctl("reload")
            assert recovered.payload["success"] is True
