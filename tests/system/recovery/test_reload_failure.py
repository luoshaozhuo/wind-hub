"""验证配置 prepare 失败不影响旧任务及后续成功激活。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import (
    get_task,
    prepare_config,
    reload_config,
    start_task_instance,
)
from tests.support.functional_config import update_yaml
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    DEVICE_ID,
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
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        await sink_service_factory(config_dir)
        proc: CollectorProcess = await collector_factory(config_dir)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        initial = await wait_telemetry_value(redis_service)

        # Collector 不解析 sinks.yaml（include_sinks=False）；故障注入必须
        # 落在 Collector 实际校验的边界——Task 引用不存在的设备组。
        def _break(data: dict[str, Any]) -> None:
            data["tasks"][TASK_ID]["device_group_id"] = "ghost-device"

        update_yaml(config_dir, "tasks.yaml", _break)
        prepared = await prepare_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-broken"
        )
        assert prepared.success is False
        assert prepared.errors

        assert proc.is_running()
        continued = await wait_telemetry_value(redis_service, after=str(initial["timestamp"]))

        def _restore(data: dict[str, Any]) -> None:
            # 恢复有效设备组，同时变更 interval——验证修复版本可正常激活。
            data["tasks"][TASK_ID]["device_group_id"] = DEVICE_ID
            data["tasks"][TASK_ID]["interval"] = 0.5

        update_yaml(config_dir, "tasks.yaml", _restore)
        activated = await reload_config(
            proc.grpc_target, config_dir=config_dir, revision_id="rev-restored"
        )
        assert activated.success is True, list(activated.errors)

        task = await get_task(proc.grpc_target, TASK_ID)
        assert task.HasField("interval")
        assert task.interval.value == 0.5
        final = await wait_telemetry_value(redis_service, after=str(continued["timestamp"]))
        assert final["quality"] == "good"
