"""Sink 服务——运行 Sink 检查与诊断写的应用编排。

基于 :class:`~wind_hub_collector.application.runtime.runtime.CollectorRuntime` 提供
gRPC 控制面所需的 Sink application operations：列出运行 Sink 健康与队列深度、
校验指定 Sink、写入一条明确标记的诊断测试点。

Sink 实例注册表、队列、背压与 lifecycle 仍由 SinkRuntime 唯一持有；本服务只
读取运行快照并触发显式诊断写，不持有任何 Sink 状态。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub_collector.application.runtime.runtime import CollectorRuntime
from wind_hub_core.model.point import PointValue


class SinkInfo(BaseModel):
    """单个运行 Sink 的健康与队列深度快照。"""

    name: str
    """Sink 稳定名称。"""

    healthy: bool = False
    """Sink 当前可正常工作时为 True。"""

    message: str = ""
    """可选状态说明或错误摘要。"""

    queue_depth: int = 0
    """当前排队待交付的点值批数。"""


class SinkWriteResult(BaseModel):
    """诊断测试写结果。"""

    success: bool
    """写入并 flush 成功时为 True。"""

    message: str = ""
    """失败时的异常摘要；成功时为空串。"""


class CollectorSinkService:
    """Collector 运行 Sink 的检查与诊断写入口。"""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

    async def list_sinks(self) -> list[SinkInfo]:
        """列出当前运行 Sink 的健康状态与队列深度。"""
        health = self._runtime.health()
        depths = self._runtime.sink_queue_depths()
        items: list[SinkInfo] = []
        for name in self._runtime.sinks:
            current = health.get(name)
            items.append(
                SinkInfo(
                    name=name,
                    healthy=bool(current.healthy) if current is not None else False,
                    message=(current.message or "") if current is not None else "",
                    queue_depth=int(depths.get(name, 0)),
                )
            )
        return items

    async def verify_sink(self, name: str) -> SinkInfo:
        """返回指定运行 Sink 的真实 health 与队列深度。

        Raises:
            KeyError: Sink 未注册。
        """
        sink = self._runtime.sinks.get(name)
        if sink is None:
            raise KeyError(name)
        health = sink.health()
        return SinkInfo(
            name=name,
            healthy=bool(health.healthy),
            message=health.message or "",
            queue_depth=int(self._runtime.sink_queue_depths().get(name, 0)),
        )

    async def write_test_sink(self, name: str) -> SinkWriteResult:
        """向指定运行 Sink 写入诊断测试点并 flush。

        Raises:
            KeyError: Sink 未注册。
        """
        sink = self._runtime.sinks.get(name)
        if sink is None:
            raise KeyError(name)
        try:
            await sink.write(
                [PointValue(device_id="_diagnostic", point_id="_write_test", value=1)]
            )
            await sink.flush()
        except Exception as exc:
            return SinkWriteResult(
                success=False,
                message=str(exc) or type(exc).__name__,
            )
        return SinkWriteResult(success=True)
