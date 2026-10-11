"""基础输出 Sink 契约，不依赖 Collector 或具体存储介质。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.application.protocol_contract import ConnectionHealth
from core.domain.point_value import PointValue


class SinkPort(Protocol):
    """批量交付工程点值的输出端口。"""

    async def open(self) -> None:
        """打开输出资源；重复调用应安全。"""
        ...

    async def close(self) -> None:
        """关闭资源；重复调用应安全。"""
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """交付一个批次；失败时抛出异常，不静默丢弃。"""
        ...

    async def flush(self) -> None:
        """提交内部缓冲；不承诺执行物理介质 fsync。"""
        ...

    async def health(self) -> ConnectionHealth:
        """执行一次无业务副作用的健康检查；不得写入虚假业务数据。"""
        ...


@runtime_checkable
class ExclusiveOpenSinkPort(Protocol):
    """需要先关闭旧实例才能打开新实例的可选能力。"""

    @property
    def exclusive_open(self) -> bool:
        """是否要求同名资源采用 close-first 重建。"""
        ...
