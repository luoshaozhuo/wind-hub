"""验证 Collector 在正常采集和设备断连时均能优雅退出。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import metrics_snapshot, start_task_instance
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import DEVICE_ID, recovery_collector_tree, wait_status
from tests.support.redis_client import point_key
from tests.support.wait import wait_redis_value, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

# v2：task_id == device_id，扁平 tasks.yaml 的任务名也必须是设备身份。
TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID
KEY_PREFIX = "wind-hub-shutdown"


def _flat_config(base: Path, port: int, redis_address: str, nats_url: str) -> Path:
    host, _, redis_port = redis_address.rpartition(":")
    return write_functional_config(
        base,
        port,
        nats_url=nats_url,
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {"host": host, "port": int(redis_port)},
                "outputs": {TASK_ID: {"key_prefix": KEY_PREFIX}},
            }
        },
        tasks={
            TASK_ID: {
                "device_group_id": DEVICE_ID,
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
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        flat_dir = _flat_config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree, shutdown_timeout=15.0)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        key = point_key(DEVICE_ID, "rotor_speed", prefix=KEY_PREFIX)
        value = await wait_redis_value(redis_service, key)
        assert value["quality"] == "good"

        # Redis 最新值不是历史档案；这里只验证已交付值和进程退出行为。
        assert proc.terminate(timeout=30.0) == 0
        assert (await wait_redis_value(redis_service, key))["timestamp"] >= value["timestamp"]

    async def test_sigterm_during_device_outage_still_exits_cleanly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        flat_dir = _flat_config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree, shutdown_timeout=15.0)

        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        # v2 采集计数在 Metrics 服务（Runtime status 不含累计点计数）。
        async def _collected() -> bool | None:
            snap = await metrics_snapshot(proc.grpc_target)
            return True if snap["counters"]["points_total"] >= 1 else None

        await wait_until(_collected, timeout=30.0, description="collection started")
        await modbus_server.stop()
        await wait_status(
            proc,
            lambda payload: payload["device_connected"] == 0,
            description="device detected disconnected",
        )
        assert proc.terminate(timeout=30.0) == 0
