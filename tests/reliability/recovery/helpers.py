"""Recovery 测试共享助手——故障注入周期（正常 → 故障 → 检测 → 存活 → 恢复）
中反复使用的边界观测原语与配置生成器。

所有观测都发生在系统边界：ctl subprocess 的 status payload、独立 Kafka
consumer、输出文件行数——不读取 Collector 进程内部状态。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.component.collector.conftest import write_functional_config
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
    params = {"path": str(sink_path), "buffer_size": 4, "flush_interval": 0.5}
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


class KafkaFlowObserver:
    """故障注入前预启动的 Kafka 流量观测器（broker 中断检测用）。

    broker 停止后新 consumer 无法 bootstrap（启动即失败），因此观测器
    必须在故障注入之前 :meth:`start`。观测分两步：

    1. :meth:`drain` —— 排空 broker 死亡前已进入 consumer 缓冲的存量。
       broker 已死，缓冲只减不增，排空是确定性的；
    2. :meth:`count_during` —— 统计窗口内新到达的消息数。这是测量而非
       就绪等待——窗口时长即断言语义的一部分。

    broker 死亡期间 consumer 拉取失败（KafkaError）即「无消息到达」的
    预期表现，计 0 并继续观测，不算吞异常。
    """

    def __init__(self, bootstrap_servers: str, topic: str) -> None:
        from aiokafka import AIOKafkaConsumer  # 延迟导入：无 kafka 环境也可收集本模块

        self._consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            group_id=f"wind-hub-recovery-{uuid.uuid4().hex[:12]}",
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )

    async def start(self) -> None:
        await self._consumer.start()

    async def drain(self, *, timeout: float = 10.0) -> None:
        """排空 consumer 缓冲中的存量消息（须在 broker 停止后调用）。

        broker 死亡后缓冲只减不增：持续拉取直到一个完整轮询周期返回空。
        超时仍排不空说明 broker 其实还在供数——调用时序错误，直接失败。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._poll(timeout_ms=1000) == 0:
                return
        raise AssertionError(
            "kafka flow did not quiesce after broker stop — "
            "drain() must be called after the broker is actually down"
        )

    async def count_during(self, window: float) -> int:
        """统计观测窗口内新到达的消息条数。"""
        deadline = time.monotonic() + window
        count = 0
        while time.monotonic() < deadline:
            count += await self._poll(timeout_ms=500)
        return count

    async def _poll(self, *, timeout_ms: int) -> int:
        from aiokafka.errors import KafkaError

        try:
            batch = await self._consumer.getmany(timeout_ms=timeout_ms)
        except KafkaError:
            # broker 死亡期间拉取失败即「无消息到达」的预期观测。
            return 0
        return sum(len(records) for records in batch.values())

    async def close(self) -> None:
        from aiokafka.errors import KafkaError

        current = asyncio.current_task()
        try:
            await self._consumer.stop()
        except asyncio.CancelledError:
            # aiokafka 0.12 fetcher.close() 等待内部 fetch 任务时，若该任务
            # 正在处理 broker 死亡引发的 NodeNotReadyError，对内部任务的取消
            # 会穿透到 stop() 的调用方。只有外层任务自身被取消（真正的测试
            # 取消）才向上传播，否则视为清理噪声——观测结论早已完成。
            if current is not None and current.cancelling() > 0:
                raise
        except KafkaError:
            # broker 尚未恢复时的关闭失败不影响已完成的观测结论。
            pass
