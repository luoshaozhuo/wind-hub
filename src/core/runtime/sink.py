"""Sink Runtime。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from core.application import PointValue
from core.application.port import SinkPort
from core.config import SinkId


class SinkRuntime:
    """SinkPort 实例及其生命周期、交付路由的唯一 owner。"""

    def __init__(self, sinks: Mapping[SinkId, SinkPort]) -> None:
        self._sinks = dict(sinks)

    @property
    def sinks(self) -> Mapping[SinkId, SinkPort]:
        """返回只读 Sink 实例索引。"""
        return MappingProxyType(self._sinks)

    async def start(self) -> None:
        """打开全部 Sink；任一失败则回滚已打开实例。"""
        opened: list[SinkPort] = []
        try:
            for sink in self._sinks.values():
                await sink.open()
                opened.append(sink)
        except Exception:
            for sink in reversed(opened):
                try:
                    await sink.close()
                except Exception:
                    pass
            raise

    async def dispatch(
        self,
        sink_ids: Sequence[SinkId],
        batch: Sequence[PointValue],
    ) -> None:
        """把同一批标准点值交付到指定 Sink。

        当前采用同步 fan-out：任一 Sink 写入失败即向调用方传播。
        队列、背压、重试、丢弃策略不在基础 Runtime 中隐式决定。
        """
        if not batch:
            return
        for sink_id in sink_ids:
            await self._sinks[sink_id].write(batch)

    async def stop(self) -> None:
        """依次 flush 并关闭全部 Sink；尽量完成所有资源释放。"""
        first_error: Exception | None = None
        for sink in reversed(tuple(self._sinks.values())):
            try:
                await sink.flush()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            try:
                await sink.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
