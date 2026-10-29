"""验证 Collector 在正常采集和设备断连时均能优雅退出。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import wait_status
from tests.support.redis_client import point_key
from tests.support.wait import wait_redis_value

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"
KEY_PREFIX = "wind-hub-shutdown"


def _config(base: Path, port: int, redis_address: str) -> Path:
    host, _, redis_port = redis_address.rpartition(":")
    return write_functional_config(
        base,
        port,
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {"host": host, "port": int(redis_port), "key_prefix": KEY_PREFIX},
                "outputs": {TASK_ID: {}},
            }
        },
        tasks={
            TASK_ID: {
                "device_group_id": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
            }
        },
    )


class TestGracefulShutdown:
    async def test_sigterm_exits_cleanly_after_output(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = _config(tmp_path / "cfg", modbus_server.port, redis_service)
        proc: CollectorProcess = await collector_factory(config_dir, shutdown_timeout=15.0)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        key = point_key("modbus-1", "rotor_speed", prefix=KEY_PREFIX)
        value = await wait_redis_value(redis_service, key)
        assert value["quality"] == "good"

        # Redis 最新值不是历史档案；这里只验证已交付值和进程退出行为。
        assert proc.terminate(timeout=30.0) == 0
        assert (await wait_redis_value(redis_service, key))["timestamp"] >= value["timestamp"]

    async def test_sigterm_during_device_outage_still_exits_cleanly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = _config(tmp_path / "cfg", modbus_server.port, redis_service)
        proc: CollectorProcess = await collector_factory(config_dir, shutdown_timeout=15.0)

        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await wait_status(
            proc,
            lambda payload: payload["points_collected"] >= 1,
            description="collection started",
        )
        await modbus_server.stop()
        await wait_status(
            proc,
            lambda payload: payload["devices_connected"] == 0,
            description="device detected disconnected",
        )
        assert proc.terminate(timeout=30.0) == 0
