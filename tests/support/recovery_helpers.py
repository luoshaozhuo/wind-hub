"""Recovery 测试共享助手——故障注入周期（正常 → 故障 → 检测 → 存活 → 恢复）
中反复使用的边界观测原语与配置生成器。

所有观测都发生在系统边界：Collector gRPC Runtime 接口的 status payload、
独立 Redis 连接——不读取 Collector 进程内部状态。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.support.control import runtime_status_snapshot
from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess
from tests.support.redis_client import point_key
from tests.support.wait import (
    WaitTimeoutError,
    read_redis_value,
    wait_redis_value,
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
    key = point_key(DEVICE_ID, point_id, prefix="wind-hub-recovery")

    def _matches(value: dict[str, Any]) -> bool:
        return (after is None or str(value["timestamp"]) > after) and (
            match is None or match(value)
        )

    return await wait_redis_value(redis_address, key, match=_matches)


async def status_snapshot(proc: CollectorProcess) -> dict[str, Any] | None:
    """经 Collector gRPC Runtime 查询状态快照；RPC 失败返回 None（继续轮询）。"""
    return await runtime_status_snapshot(proc.grpc_target)


async def wait_status(
    proc: CollectorProcess,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 30.0,
    description: str = "status condition",
) -> dict[str, Any]:
    """轮询 gRPC Runtime status 直到 ``predicate(payload)`` 成立，返回该快照。

    超时消息带最近一次观测到的 payload 摘要——「条件没等到」与
    「RPC 一直失败」在诊断上是两种完全不同的问题。
    """
    last_seen: dict[str, Any] | None = None

    async def _probe() -> dict[str, Any] | None:
        nonlocal last_seen
        payload = await status_snapshot(proc)
        if payload is None:
            return None
        last_seen = payload
        if not predicate(payload):
            return None
        return payload

    try:
        return await wait_until(_probe, timeout=timeout, interval=0.5, description=description)
    except WaitTimeoutError as exc:
        if last_seen is not None:
            summary = {
                k: last_seen.get(k)
                for k in (
                    "running",
                    "devices_connected",
                    "sinks_healthy",
                    "points_collected",
                    "points_routed",
                    "points_dropped",
                )
            }
            raise WaitTimeoutError(f"{exc}; last status snapshot: {summary}") from exc
        raise


class RedisFlowObserver:
    """Redis 点值更新观测器（服务中断检测用）。

    Redis sink 以 SET 覆盖各点最新值，负载带采集 timestamp：服务存活
    期间同一 key 的 timestamp 随采集周期持续推进；服务停止后 timestamp
    冻结。观测分两步：

    1. :meth:`wait_quiesced` —— 确认 timestamp 已不再变化（故障已传导
       到系统边界；须在服务停止后调用）；
    2. :meth:`count_during` —— 统计窗口内 timestamp 的变化次数。这是
       测量而非就绪等待——窗口时长即断言语义的一部分。

    服务死亡期间 GET 连接失败即「无更新到达」的预期表现，计 0 并继续
    观测，不算吞异常。每次读取独立建连——观测器与被测 Collector 的
    连接状态完全解耦。
    """

    def __init__(self, address: str, key: str) -> None:
        self._address = address
        self._key = key

    async def _read_ts(self) -> str | None:
        try:
            value = await read_redis_value(self._address, self._key)
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            # 服务死亡期间读取失败即「无更新到达」的预期观测。
            return None
        return None if value is None else str(value.get("timestamp"))

    async def wait_quiesced(self, *, timeout: float = 10.0) -> None:
        """等待 timestamp 冻结（须在服务停止后调用）。

        服务死亡后 key 不再被 SET：轮询直到相邻两次读取（间隔大于采集
        周期）完全一致。超时仍在推进说明服务其实还在供数——调用时序
        错误，直接失败。
        """
        deadline = time.monotonic() + timeout
        last = await self._read_ts()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            current = await self._read_ts()
            if current == last:
                return
            last = current
        raise AssertionError(
            "redis flow did not quiesce after service stop — "
            "wait_quiesced() must be called after the service is actually down"
        )

    async def count_during(self, window: float) -> int:
        """统计观测窗口内 timestamp 的变化次数。"""
        deadline = time.monotonic() + window
        count = 0
        last = await self._read_ts()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            current = await self._read_ts()
            if current is not None and last is not None and current != last:
                count += 1
            if current is not None:
                last = current
        return count
