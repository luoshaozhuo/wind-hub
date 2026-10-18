"""Recovery 测试共享助手——故障注入周期（正常 → 故障 → 检测 → 存活 → 恢复）
中反复使用的边界观测原语与配置生成器。

所有观测都发生在系统边界：CLI subprocess 的 status payload、独立 Redis
连接——不读取 Collector 进程内部状态。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess, run_cli_async
from tests.support.wait import (
    WaitTimeoutError,
    wait_until,
)

#: recovery 基线拓扑的固定身份（functional_config 的默认任务/设备）。
TASK_ID = "modbus-telemetry"
DEVICE_ID = "modbus-1"


def telemetry_task(
    sink_id: str,
    *,
    task_id: str = TASK_ID,
    key_prefix: str | None = None,
) -> dict[str, Any]:
    """单设备 telemetry 采集任务定义（新契约：outputs[].sink_id）。"""
    output: dict[str, Any] = {"sink_id": sink_id}
    if key_prefix is not None:
        output["key_prefix"] = key_prefix
    return {
        task_id: {
            "device_group_id": DEVICE_ID,
            "point_group": "telemetry",
            "interval": 0.2,
            "outputs": [output],
        }
    }


def telemetry_multi_task(sink_ids: list[str], *, task_id: str = TASK_ID) -> dict[str, Any]:
    """单任务多 Sink 扇出（每 Sink 一个 output）。"""
    return {
        task_id: {
            "device_group_id": DEVICE_ID,
            "point_group": "telemetry",
            "interval": 0.2,
            "outputs": [{"sink_id": sink_id} for sink_id in sink_ids],
        }
    }


def write_modbus_redis_config(base: Path, port: int, redis_address: str) -> Path:
    """单 Modbus 设备和 Redis 最新值输出，用于进程级恢复测试。"""
    host, _, redis_port = redis_address.rpartition(":")
    return write_functional_config(
        base,
        port,
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {"host": host, "port": int(redis_port)},
            }
        },
        tasks=telemetry_task("redis_sink", key_prefix="wind-hub-recovery"),
    )


async def wait_telemetry_value(
    redis_address: str,
    *,
    point_id: str = "rotor.speed",
    after: str | None = None,
    match: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """等待 Redis 最新值；after 仅判断严格更新，不表示历史行数。"""
    from tests.support.redis_client import point_key
    from tests.support.wait import wait_redis_value

    key = point_key(DEVICE_ID, point_id, prefix="wind-hub-recovery")

    def _matches(value: dict[str, Any]) -> bool:
        return (after is None or str(value["timestamp"]) > after) and (
            match is None or match(value)
        )

    return await wait_redis_value(redis_address, key, match=_matches)



