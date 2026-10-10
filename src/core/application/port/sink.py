"""跨应用共享的 Sink outbound port。

仅定义批量数据交付、生命周期及健康状态；Sink 适配器、注册表、
数据路由、缓冲和重试策略由使用方负责。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.application.protocol_contract import ConnectionHealth
from core.domain.point_value import PointValue


class SinkPort(Protocol):
    """Sink 的最小异步交付契约。"""

    async def open(self) -> None:
        """初始化 Sink 资源。"""
        ...

    async def close(self) -> None:
        """幂等关闭 Sink 资源。"""
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """交付一批已路由的工程点值。"""
        ...

    async def flush(self) -> None:
        """将 Sink 内部缓冲提交到底层介质。"""
        ...

    def health(self) -> ConnectionHealth:
        """读取缓存的健康状态，不执行阻塞 I/O。"""
        ...


@runtime_checkable
class ExclusiveOpenSinkPort(Protocol):
    """标识替换实例时需要先释放原资源的可选能力。"""

    @property
    def exclusive_open(self) -> bool:
        """是否要求先关闭旧实例才能打开新实例。"""
        ...
