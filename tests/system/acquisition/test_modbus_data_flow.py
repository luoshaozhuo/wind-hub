"""System E2E：Modbus → Collector 子进程 → NATS → Sink Service 子进程 → Redis。

新架构下 Collector 只采集并发布到 NATS（realtime Core subject
``wh.rt.<farm>.<collector>.<device>.<point>``），独立的 sink_service 进程
消费并写 Redis。两个工作进程都以 console script 真实 subprocess 运行，
任务启停经 Collector gRPC 控制面下发（v2 设备粒度 START/STOP，与生产路径
一致）；断言全部发生在系统边界——独立 NATS / Redis 连接，不读取任一进程
内部状态。Collector 只消费 v2 三路径树（由扁平站点派生），Sink 仍读扁平
目录；两侧以 v2 身份 ``task_id == device_id == "modbus-1"`` 对齐。

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
from tests.support.collector_config_tree import build_collector_tree_from_site
from tests.support.control import start_task_instance, stop_task
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.sink_harness import latest_value_key
from tests.support.wait import read_redis_value, wait_redis_value

pytestmark = [pytest.mark.redis, pytest.mark.real_service]

# v2：task_id == device_id == instance_id。
TASK_ID = "modbus-1"
DEVICE_ID = "modbus-1"
INSTANCE_ID = DEVICE_ID

#: 本文件 Redis 命名空间（新架构键契约 ``{namespace}:{farm}:{collector}:{device}:{point}``）。
NAMESPACE = "wind-hub-sys"
#: 采集身份须与 Sink Delivery source 过滤器一致。
COLLECTOR_ID = "collector"


def _sys_key(point_id: str) -> str:
    return latest_value_key(
        NAMESPACE, collector_id=COLLECTOR_ID, device_id=DEVICE_ID, point_id=point_id
    )


def _flat_task() -> dict[str, Any]:
    return {
        TASK_ID: {
            "device_group_id": DEVICE_ID,
            # 扁平 Core 校验器要求点组真实存在；rotor_speed/gen_power/
            # temp_int/setpoint_power 均归属 telemetry。
            "point_group": "telemetry",
            "interval": 0.2,
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
        flat_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            tasks=_flat_task(),
            nats_url=nats_service,
        )
        # 先起消费侧（sink_service，canonical 配置根），再起生产侧（Collector）。
        sink_proc = await sink_service_factory(namespace=NAMESPACE, collector_id=COLLECTOR_ID)
        assert sink_proc.is_running()
        v2_tree = build_collector_tree_from_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        collector = await collector_factory(v2_tree, collector_id="collector")

        await start_task_instance(collector.grpc_target, instance_id=INSTANCE_ID)

        # Redis sink 每点一个 key 覆盖最新值——验证值内容、时间戳在推进、
        # 且同组其余点位同样经 NATS 出数。
        key = _sys_key("rotor_speed")
        first = await wait_redis_value(redis_service, key)
        assert first["value"] == pytest.approx(1200.5)
        assert first["quality"] == "good"
        first_ts = first["sample_time"]

        later = await wait_redis_value(
            redis_service, key, match=lambda v: v["sample_time"] > first_ts
        )
        assert later["value"] == pytest.approx(1200.5)

        for point_id in ("gen_power", "temp_int", "setpoint_power"):
            value = await wait_redis_value(redis_service, _sys_key(point_id))
            assert value["quality"] == "good"
            assert value["sample_time"]

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
        flat_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            tasks=_flat_task(),
            nats_url=nats_service,
        )
        await sink_service_factory(namespace=NAMESPACE, collector_id=COLLECTOR_ID)
        v2_tree = build_collector_tree_from_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        collector = await collector_factory(v2_tree, collector_id="collector")
        await start_task_instance(collector.grpc_target, instance_id=INSTANCE_ID)
        key = _sys_key("rotor_speed")
        initial = await wait_redis_value(redis_service, key)
        await wait_redis_value(
            redis_service,
            key,
            match=lambda item: item["sample_time"] > initial["sample_time"],
        )

        await stop_task(collector.grpc_target, DEVICE_ID)
        # 允许已在 NATS / sink 管道中的在途样本排空，再观察连续多个采集周期。
        await asyncio.sleep(1.0)
        settled = await read_redis_value(redis_service, key)
        assert settled is not None
        await asyncio.sleep(1.0)
        after = await read_redis_value(redis_service, key)
        assert after is not None
        assert after["sample_time"] == settled["sample_time"]
