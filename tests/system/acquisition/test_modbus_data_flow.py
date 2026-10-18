"""System E2E：Modbus → Collector 子进程 → 真实 sink 的数据面验收。

链路：真实 Modbus TCP 从站 → Collector subprocess（console script 启动）
→ 真实 Redis sink。任务启停经 CollectorControlService RPC
下发（与 Server → Collector 生产控制路径一致）；验证全部发生在系统
边界——独立 Redis 连接，不读取 Collector 进程内部状态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.redis_client import point_key
from tests.support.wait import (
    wait_redis_value,
)

pytestmark = pytest.mark.real_service

TASK_ID = "modbus-telemetry"
DEVICE_ID = "modbus-1"
INSTANCE_ID = f"{TASK_ID}:{DEVICE_ID}"

#: 本文件 Redis sink 的 key_prefix（键规则见 tests.support.redis_client.point_key）。
REDIS_KEY_PREFIX = "wind-hub-sys"


def _task(sink_id: str, *, key_prefix: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {"sink_id": sink_id}
    if key_prefix is not None:
        output["key_prefix"] = key_prefix
    return {
        TASK_ID: {
            "device_group_id": DEVICE_ID,
            "point_group": "telemetry",
            "interval": 0.2,
            "outputs": [output],
        }
    }


@pytest.mark.redis
class TestModbusToRedis:
    async def test_collected_points_reach_redis(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        redis_service: str,
        tmp_path: Path,
    ) -> None:
        config_dir = write_functional_config(
            tmp_path / "cfg",
            modbus_server.port,
            sinks={
                "redis_sink": {
                    "type": "redis",
                    "connection": {
                        "host": redis_service.rpartition(":")[0],
                        "port": int(redis_service.rpartition(":")[2]),
                    },
                }
            },
            tasks=_task("redis_sink", key_prefix=REDIS_KEY_PREFIX),
        )
        proc: CollectorProcess = await collector_factory(config_dir)

        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        # Redis sink 每点一个 key 覆盖最新值——验证值内容、时间戳在推进、
        # 且同组其余点位同样出数。
        key = point_key("modbus-1", "rotor.speed", prefix=REDIS_KEY_PREFIX)
        first = await wait_redis_value(redis_service, key)
        assert first["value"] == pytest.approx(1200.5)
        assert first["quality"] == "good"
        first_ts = first["timestamp"]

        later = await wait_redis_value(
            redis_service, key, match=lambda v: v["timestamp"] > first_ts
        )
        assert later["value"] == pytest.approx(1200.5)

        for point_id in ("gen.power", "temp.int", "setpoint.power"):
            value = await wait_redis_value(
                redis_service, point_key("modbus-1", point_id, prefix=REDIS_KEY_PREFIX)
            )
            assert value["quality"] == "good"
            assert value["timestamp"]
