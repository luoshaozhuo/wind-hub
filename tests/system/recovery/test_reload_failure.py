"""验证配置 prepare 失败不影响旧任务及后续成功激活。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import prepare_config, reload_config, start_task_instance
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess, run_cli_async
from tests.support.recovery_helpers import (
    wait_telemetry_value,
    write_modbus_redis_config,
)

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"


class TestReloadFailureResilience:
    async def test_failed_reload_keeps_running_and_recovers(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = write_modbus_redis_config(tmp_path / "cfg", modbus_server.port, redis_service)
        proc: CollectorProcess = await collector_factory(config_dir)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        initial = await wait_telemetry_value(redis_service)

        update_yaml(
            config_dir,
            "sinks.yaml",
            lambda data: data["sinks"]["redis_sink"]["outputs"].update({"ghost_task": {}}),
        )
        prepared = await prepare_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-broken"
        )
        assert prepared.success is False
        assert prepared.errors

        assert proc.is_running()
        continued = await wait_telemetry_value(redis_service, after=str(initial["timestamp"]))

        def _restore(data: dict[str, Any]) -> None:
            data["tasks"][TASK_ID]["interval"] = 0.5

        update_yaml(config_dir, "tasks.yaml", _restore)
        update_yaml(
            config_dir,
            "sinks.yaml",
            lambda data: data["sinks"]["redis_sink"]["outputs"].pop("ghost_task"),
        )
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-restored"
        )
        assert activated.success is True, list(activated.errors)

        task = await run_cli_async("task", TASK_ID, target=proc.grpc_target)
        assert json.loads(task.stdout)["interval"] == 0.5
        final = await wait_telemetry_value(redis_service, after=str(continued["timestamp"]))
        assert final["quality"] == "good"
