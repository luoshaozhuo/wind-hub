"""Modbus 断连与恢复：通过真实 Collector 进程和 Redis 最新值观测。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    status_snapshot,
    wait_status,
    wait_telemetry_value,
    write_modbus_redis_config,
)

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

# v2：task_id == device_id == instance_id == "modbus-1"。
TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID


class TestProtocolDisconnectRecovery:
    async def test_full_disconnect_recovery_cycle(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        flat_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        await sink_service_factory(flat_dir)
        v2_tree = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        first = await wait_telemetry_value(redis_service)
        assert first["quality"] == "good"

        await modbus_server.stop()
        await wait_status(
            proc,
            lambda status: status["device_connected"] == 0,
            description="device detected disconnected",
        )

        # 允许最后的在途批次落地，随后检查 Redis 最新值不再刷新。
        await asyncio.sleep(1.0)
        settled = await wait_telemetry_value(redis_service)
        await asyncio.sleep(0.8)
        assert (await wait_telemetry_value(redis_service))["timestamp"] == settled["timestamp"]

        assert proc.is_running()
        status = await status_snapshot(proc)
        assert status is not None
        assert status["running"] is True

        await modbus_server.start()
        await wait_status(
            proc,
            lambda status: status["device_connected"] == 1,
            description="device reconnected",
        )
        rotor = await wait_telemetry_value(redis_service, after=str(settled["timestamp"]))
        assert rotor["value"] == pytest.approx(1200.5)
        assert rotor["quality"] == "good"
