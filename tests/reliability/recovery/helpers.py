"""Recovery 测试共享助手——故障注入周期（正常 → 故障 → 检测 → 存活 → 恢复）
中反复使用的边界观测原语与配置生成器。

所有观测都发生在系统边界：ctl subprocess 的 status payload、独立 Redis
连接、输出文件行数——不读取 Collector 进程内部状态。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.support.functional_config import write_functional_config
from tests.support.process import CollectorProcess, run_ctl_async
from tests.support.wait import WaitTimeoutError, wait_until


def telemetry_task(sink_name: str, *, task_id: str = "modbus-telemetry") -> dict[str, Any]:
    """单设备 telemetry 采集任务定义（指向指定 sink）。"""
    return {
        "task_id": task_id,
        "device": "modbus-1",
        "point_group": "telemetry",
        "interval": 0.2,
        "targets": [{"sink": sink_name}],
    }


def write_modbus_file_config(
    base: Path,
    port: int,
    sink_path: Path,
    **sink_params: Any,
) -> Path:
    """单 Modbus 设备 + File sink 的现场配置（recovery 各用例的基线拓扑）。"""
    params = {"path": str(sink_path)}
    params.update(sink_params)
    return write_functional_config(
        base,
        port,
        sinks=[{"name": "file_sink", "type": "file", "connection": params}],
        tasks=[telemetry_task("file_sink")],
    )


async def ctl_status(proc: CollectorProcess) -> dict[str, Any] | None:
    """经 ctl subprocess 查询 Runtime status；RPC 失败返回 None（继续轮询）。"""
    result = await run_ctl_async("status", target=proc.grpc_target)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


async def ctl_instance_states(proc: CollectorProcess) -> dict[str, str] | None:
    """经 ctl task-instances 查询实例状态表（instance_id → state）。

    与 status 的 ``acquisitions[].running``（一次 collect 执行中的瞬态
    标志）不同，这是实例的生命周期状态（RUNNING/STOPPED）——任务控制
    断言必须用它。RPC 失败返回 None（继续轮询）。
    """
    result = await run_ctl_async("task-instances", target=proc.grpc_target)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return {item["instance_id"]: item["state"] for item in json.loads(result.stdout)["items"]}


async def wait_status(
    proc: CollectorProcess,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 30.0,
    description: str = "status condition",
) -> dict[str, Any]:
    """轮询 ctl status 直到 ``predicate(payload)`` 成立，返回该快照。

    超时消息带最近一次观测到的 payload 摘要——「条件没等到」与
    「RPC 一直失败」在诊断上是两种完全不同的问题。
    """
    last_seen: dict[str, Any] | None = None

    async def _probe() -> dict[str, Any] | None:
        nonlocal last_seen
        payload = await ctl_status(proc)
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
        from tests.support.wait import read_redis_value

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
