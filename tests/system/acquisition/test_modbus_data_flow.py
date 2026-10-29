"""System E2E：Modbus → Collector 子进程 → NATS → Sink Service 子进程 → Redis。

新架构下 Collector 只采集并发布到 NATS（realtime Core subject
``wh.rt.<farm>.<collector>.<device>.<point>``），独立的 sink_service 进程
消费并写 Redis。两个工作进程都以 console script 真实 subprocess 运行，
任务启停经 Collector gRPC 控制面下发（与 Server → Collector 生产路径一致）；
断言全部发生在系统边界——独立 NATS / Redis 连接，不读取任一进程内部状态。

外部依赖：真实 Modbus 从站（fixture）、真实 NATS（``WIND_HUB_TEST_NATS``，
默认 127.0.0.1:4222）、真实 Redis（``redis_service``）。任一不可达明确 skip，
绝不以内存替身冒充真实链路。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance, stop_instance
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.redis_client import point_key
from tests.support.wait import read_redis_value, wait_redis_value

pytestmark = [pytest.mark.redis, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
DEVICE_ID = "modbus-1"
INSTANCE_ID = f"{TASK_ID}:{DEVICE_ID}"

#: 本文件 Redis sink 的 key_prefix（键规则见 tests.support.redis_client.point_key）。
REDIS_KEY_PREFIX = "wind-hub-sys"
SINK_ID = "redis_sink"


def _redis_sink(host: str, port: int) -> dict[str, Any]:
    """绑定采集 Task 的 Redis Sink；空 outputs 映射=该 Task 全量点组。"""
    return {
        SINK_ID: {
            "type": "redis",
            "connection": {
                "host": host,
                "port": port,
                "key_prefix": REDIS_KEY_PREFIX,
            },
            "outputs": {TASK_ID: {}},
        }
    }


@pytest.mark.redis
class TestModbusToNatsToRedis:
    async def test_collected_points_reach_redis_via_nats(
        self,
        modbus_server: ModbusMockServer,
        collector_factory: Callable[..., Awaitable[CollectorProcess]],
        sink_service_factory: Callable[..., Awaitable[CollectorProcess]],
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        redis_host, _, redis_port = redis_service.rpartition(":")
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=_redis_sink(redis_host, int(redis_port)),
            nats_url=nats_service,
        )
        # 先起消费侧（sink_service），再起生产侧（Collector），避免首拍丢失。
        sink_proc = await sink_service_factory(config_dir)
        assert sink_proc.is_running()
        collector = await collector_factory(config_dir, collector_id="collector")

        await start_task_instance(collector.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        # Redis sink 每点一个 key 覆盖最新值——验证值内容、时间戳在推进、
        # 且同组其余点位同样经 NATS 出数。
        key = point_key(DEVICE_ID, "rotor_speed", prefix=REDIS_KEY_PREFIX)
        first = await wait_redis_value(redis_service, key)
        assert first["value"] == pytest.approx(1200.5)
        assert first["quality"] == "good"
        first_ts = first["timestamp"]

        later = await wait_redis_value(
            redis_service, key, match=lambda v: v["timestamp"] > first_ts
        )
        assert later["value"] == pytest.approx(1200.5)

        for point_id in ("gen_power", "temp_int", "setpoint_power"):
            value = await wait_redis_value(
                redis_service, point_key(DEVICE_ID, point_id, prefix=REDIS_KEY_PREFIX)
            )
            assert value["quality"] == "good"
            assert value["timestamp"]

    async def test_stop_task_prevents_further_redis_updates(
        self,
        modbus_server: ModbusMockServer,
        collector_factory: Callable[..., Awaitable[CollectorProcess]],
        sink_service_factory: Callable[..., Awaitable[CollectorProcess]],
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        """任务停止后，经 NATS 到 Redis 的最新值时间戳必须保持稳定。"""
        redis_host, _, redis_port = redis_service.rpartition(":")
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks=_redis_sink(redis_host, int(redis_port)),
            nats_url=nats_service,
        )
        await sink_service_factory(config_dir)
        collector = await collector_factory(config_dir, collector_id="collector")
        await start_task_instance(collector.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
        key = point_key(DEVICE_ID, "rotor_speed", prefix=REDIS_KEY_PREFIX)
        initial = await wait_redis_value(redis_service, key)
        await wait_redis_value(
            redis_service,
            key,
            match=lambda item: item["timestamp"] > initial["timestamp"],
        )

        await stop_instance(collector.grpc_target, INSTANCE_ID)
        # 允许已在 NATS / sink 管道中的在途样本排空，再观察连续多个采集周期。
        await asyncio.sleep(1.0)
        settled = await read_redis_value(redis_service, key)
        assert settled is not None
        await asyncio.sleep(1.0)
        after = await read_redis_value(redis_service, key)
        assert after is not None
        assert after["timestamp"] == settled["timestamp"]
