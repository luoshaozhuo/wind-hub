"""R5-6：跨服务故障矩阵与资源释放（真实 Docker 服务 + 真实子进程）。

- NATS 网络隔离/恢复：Sink 订阅由 supervisor 自动修复，恢复后新消息落库；
- Redis 连续三轮停/启：连接/FD 不泄漏，每轮恢复后自动续写；
- Collector 与 Sink Service 独立进程：Collector 重启不影响 Sink 生命周期，
  恢复后链路继续出数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance
from tests.support.process import CollectorProcess, free_port
from tests.support.redis_client import point_key
from tests.support.wait import read_redis_value, wait_redis_value, wait_until
from tests.system.r5.conftest import DEVICE_ID, make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import INSTANCE_ID, TASK_ID, write_r5_config

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.reliability,
]

POINT = "rotor_speed"


def _redis_sink(host: str, port: int, *, prefix: str) -> dict[str, Any]:
    return {
        "redis_sink": {
            "type": "redis",
            "connection": {"host": host, "port": port, "key_prefix": prefix, "timeout": 1.0},
            "outputs": {TASK_ID: {}},
        }
    }


async def test_nats_network_isolation_then_subscription_repaired(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    host, _, port = r5_stack.redis.rpartition(":")
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, int(port), prefix="wh-r5-nats"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")
    await publish(r5_stack, make_sample(POINT, 1.0, sample_id="nats-before"), "realtime")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-nats")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["sample_id"] == "nats-before")

    # 容器级故障（不碰宿主机网卡）：nats-server 优雅退出立即发送 FIN，
    # 客户端连接确定性死亡（单纯 network disconnect 对空闲 TCP 不可见）。
    r5_stack.stop_container("nats")
    try:

        async def _disconnected() -> bool | None:
            status = await mgmt(sink, "GetSinkStatus", "redis_sink")
            if not status["nats_connected"]:
                return True
            return None

        await wait_until(_disconnected, timeout=20.0, interval=0.5, description="nats disconnected")
    finally:
        r5_stack.start_container("nats")

    # repair_subscription 由 supervisor 每 5s 触发；恢复后新消息到达。
    async def _reconnected() -> bool | None:
        status = await mgmt(sink, "GetSinkStatus", "redis_sink")
        if status["nats_connected"]:
            return True
        return None

    await wait_until(_reconnected, timeout=40.0, interval=0.5, description="nats reconnected")
    await publish(r5_stack, make_sample(POINT, 2.0, sample_id="nats-after"), "realtime")
    value = await wait_redis_value(
        r5_stack.redis, key, timeout=30.0, match=lambda v: v["sample_id"] == "nats-after"
    )
    assert value["value"] == 2.0


async def test_repeated_redis_outages_no_fd_leak(r5_sink_factory, r5_stack, tmp_path: Path) -> None:
    host, _, port = r5_stack.redis.rpartition(":")
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, int(port), prefix="wh-r5-fd"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-fd")

    # 基线：先完成一次写/读，消除懒连接造成的初始波动。
    await publish(r5_stack, make_sample(POINT, 0.0, sample_id="fd-base"), "realtime")
    await wait_redis_value(r5_stack.redis, key)
    import asyncio

    await asyncio.sleep(2.0)
    baseline = sink.fd_count()

    for cycle in range(1, 4):
        r5_stack.stop_container("redis")
        await publish(
            r5_stack,
            make_sample(POINT, 100.0 + cycle, sample_id=f"fd-out-{cycle}"),
            "realtime",
        )

        async def _failed() -> bool | None:
            status = await mgmt(sink, "GetSinkStatus", "redis_sink")
            return True if status["state"] == "failed" else None

        await wait_until(_failed, timeout=10.0, interval=0.3, description=f"cycle {cycle} failed")
        r5_stack.start_container("redis")
        await wait_state(sink, "redis_sink", "running", timeout=60.0)
        await publish(
            r5_stack,
            make_sample(POINT, 200.0 + cycle, sample_id=f"fd-back-{cycle}"),
            "realtime",
        )
        await wait_redis_value(
            r5_stack.redis,
            key,
            timeout=30.0,
            match=lambda v, c=cycle: v["sample_id"] == f"fd-back-{c}",
        )
        await asyncio.sleep(1.0)

    after = sink.fd_count()
    # 允许少量抖动，但连续三轮断连重建不得线性泄漏文件描述符。
    assert after <= baseline + 4, f"FD growth after 3 outage cycles: {baseline} -> {after}"


async def test_collector_restart_independent_from_sink_service(
    modbus_server: ModbusMockServer,
    collector_factory,
    r5_sink_factory,
    r5_stack,
    tmp_path: Path,
) -> None:
    host, _, port = r5_stack.redis.rpartition(":")
    config_dir = write_r5_config(
        tmp_path / "cfg",
        modbus_server.port,
        r5_stack.nats,
        _redis_sink(host, int(port), prefix="wh-r5-procs"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-procs")

    collector_a: CollectorProcess = await collector_factory(config_dir, collector_id="collector-r5")
    await start_task_instance(collector_a.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
    first = await wait_redis_value(r5_stack.redis, key, timeout=30.0)

    # Collector 终止：Sink Service 进程与状态不受影响。
    collector_a.terminate()
    import asyncio

    await asyncio.sleep(1.0)
    status = await mgmt(sink, "GetSinkStatus", "redis_sink")
    assert status["state"] == "running"
    assert sink.is_running()

    # 新 Collector 进程复用同一配置继续发布，链路恢复出数（时间戳推进）。
    collector_b: CollectorProcess = await collector_factory(config_dir, collector_id="collector-r5")
    await start_task_instance(collector_b.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

    async def _advances() -> dict[str, Any] | None:
        current = await read_redis_value(r5_stack.redis, key)
        if current is not None and current["timestamp"] > first["timestamp"]:
            return current
        return None

    later = await wait_until(_advances, timeout=30.0, description="post-restart sample")
    assert later["quality"] == "good"
    assert sink.is_running()
