"""Recovery：短周期故障/重载循环的资源稳定性——不得单调泄漏。

与 soak 的长时资源采样的区别：这里针对的是故障注入路径本身
（reconnect 循环、reload 循环）——这些路径涉及连接重建、协议实例
替换、acquisition handle 重启、sink consumer 重建，是句柄泄漏的
高发区。循环次数有限（10 次），断言的是「不单调增长」而非长时平稳。

观测点（全部在进程边界）：
- ``/proc/<pid>/fd`` 采样（含 socket/文件/管道）；
- CLI status 的 acquisitions 列表（Task Instance 唯一、无重复 handle）；
- GetMetricsSnapshot 的 reconnects 计数（确认故障循环真实发生）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import (
    metrics_snapshot,
    reload_config,
    start_task_instance,
)
from tests.support.functional_config import update_yaml, write_functional_config
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    cli_instance_states,
    wait_status,
)
from tests.support.redis_client import point_key
from tests.support.wait import wait_redis_value

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"

CYCLES = 10
#: FD 采样容忍的瞬时波动（日志 flush、metrics 连接等）；泄漏是单调增长，
#: 10 次循环后仍落在基线 +3 以内即不成立。
FD_SLACK = 3


KEY_PREFIX = "wind-hub-resources"


def _config(base: Path, device_port: int, redis_address: str, nats_url: str) -> Path:
    host, _, port = redis_address.rpartition(":")
    return write_functional_config(
        base,
        device_port,
        nats_url=nats_url,
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {"host": host, "port": int(port)},
                "outputs": {TASK_ID: {"key_prefix": KEY_PREFIX}},
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


async def _wait_data(address: str, *, after: str | None = None) -> str:
    key = point_key("modbus-1", "rotor_speed", prefix=KEY_PREFIX)
    value = await wait_redis_value(
        address,
        key,
        match=(lambda payload: payload["timestamp"] > after) if after else None,
    )
    return str(value["timestamp"])


async def _single_running_instance(proc: CollectorProcess) -> None:
    """断言本用例实例唯一且处于 RUNNING 生命周期状态。"""
    states = await cli_instance_states(proc)
    assert states is not None
    assert states.get(INSTANCE_ID) == "running", f"unexpected instance states: {states}"


class TestReconnectFlapStability:
    async def test_ten_disconnect_cycles_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RES-01：10 次断连/恢复循环后实例唯一、FD 不增长。"""
        config_dir = _config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        await sink_service_factory(config_dir)
        proc: CollectorProcess = await collector_factory(config_dir)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await _wait_data(redis_service)

        baseline_reconnects = (await metrics_snapshot(proc.grpc_target))["device_reconnects"].get(
            "modbus-1", 0
        )
        baseline_fd = proc.fd_count()

        for cycle in range(CYCLES):
            await modbus_server.stop()
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 0,
                description=f"cycle {cycle}: disconnected",
            )
            await modbus_server.start()
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 1,
                description=f"cycle {cycle}: reconnected",
            )
            await _single_running_instance(proc)

        # 数据面在最后一次恢复后继续出数。
        previous = await _wait_data(redis_service)
        await _wait_data(redis_service, after=previous)

        reconnects = (await metrics_snapshot(proc.grpc_target))["device_reconnects"].get(
            "modbus-1", 0
        )
        assert (
            reconnects - baseline_reconnects >= CYCLES
        ), f"expected >= {CYCLES} reconnects, got {reconnects - baseline_reconnects}"
        final_fd = proc.fd_count()
        assert (
            final_fd <= baseline_fd + FD_SLACK
        ), f"fd count grew from {baseline_fd} to {final_fd} over {CYCLES} flap cycles"


class TestReloadStability:
    async def test_ten_reloads_do_not_leak(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """RES-02：10 次 reload（task 重启 + sink 重建）后实例唯一、FD 不增长。

        每轮同时变更 task interval（触发 acquisition handle 重启）与 sink
        Redis timeout（触发 sink 重建、consumer 协程替换）——两条重建路径
        都是 asyncio task / 句柄泄漏高发区。
        """
        config_dir = _config(tmp_path / "cfg", modbus_server.port, redis_service, nats_service)
        await sink_service_factory(config_dir)
        proc: CollectorProcess = await collector_factory(config_dir)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        await _wait_data(redis_service)
        baseline_fd = proc.fd_count()

        for cycle in range(CYCLES):
            interval = 0.2 if cycle % 2 == 0 else 0.3
            timeout = 1.5 if cycle % 2 == 0 else 1.0

            def _mutate_tasks(data: dict[str, Any], iv: float = interval) -> None:
                data["tasks"][TASK_ID]["interval"] = iv

            def _mutate_sinks(data: dict[str, Any], value: float = timeout) -> None:
                data["sinks"]["redis_sink"]["connection"]["timeout"] = value

            update_yaml(config_dir, "tasks.yaml", _mutate_tasks)
            update_yaml(config_dir, "sinks.yaml", _mutate_sinks)
            activated = await reload_config(
                proc.grpc_target, config_dir=config_dir, revision_id=f"rev-leak-{cycle}"
            )
            assert activated.success, f"cycle {cycle} activate failed: {list(activated.errors)}"
            await _single_running_instance(proc)

        # 采集在最后一轮配置下继续出数。
        previous = await _wait_data(redis_service)
        await _wait_data(redis_service, after=previous)
        await asyncio.sleep(1.0)  # 等最后一轮重建的旧句柄完成释放

        final_fd = proc.fd_count()
        assert (
            final_fd <= baseline_fd + FD_SLACK
        ), f"fd count grew from {baseline_fd} to {final_fd} over {CYCLES} reloads"
