"""Sink outbound port。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..measurement import PointValue


class SinkPort(Protocol):
    """Application 向外部介质交付标准点值的能力边界。"""

    async def open(self) -> None:
        """初始化外部资源。"""
        ...

    async def close(self) -> None:
        """释放外部资源；实现必须支持幂等关闭。"""
        ...

    async def write(self, batch: Sequence[PointValue]) -> None:
        """写入一批已完成业务映射与单位归一化的 PointValue。"""
        ...

    async def flush(self) -> None:
        """强制提交实现内部缓冲。"""
        ...


@runtime_checkable
class ExclusiveOpenSinkPort(Protocol):
    """可选能力：重建时必须先关闭旧实例再打开新实例。"""

    @property
    def exclusive_open(self) -> bool:
        """是否要求 close-first 的替换策略。"""
        ...
