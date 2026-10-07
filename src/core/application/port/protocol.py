"""共享设备协议 outbound port。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from core.domain import ProtocolPoint

from ..config import DeviceConnection
from ..measurement import PointScalar, ProtocolSample


@dataclass(frozen=True, slots=True)
class ProtocolWrite:
    """一次协议点写入请求。"""

    point: ProtocolPoint
    value: PointScalar


@dataclass(frozen=True, slots=True)
class ProtocolWriteResult:
    """一次协议点写入结果。"""

    point_id: str
    success: bool
    message: str | None = None

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        object.__setattr__(self, "point_id", point_id)


class ProtocolPort(Protocol):
    """Collector 与 Commander 共享的最小设备通信能力边界。

    本接口只表达一次连接上的基础通信能力，不包含轮询调度、订阅生命周期、
    总召、重连退避等具体应用运行策略。
    """

    async def connect(self) -> None:
        """建立底层协议连接。"""
        ...

    async def close(self) -> None:
        """关闭底层协议连接；实现必须支持幂等调用。"""
        ...

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        """批量读取协议点。"""
        ...

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """批量写入协议点。"""
        ...


class ProtocolFactoryPort(Protocol):
    """按共享连接配置创建协议 Adapter 的工厂端口。"""

    def create(
        self,
        connection: DeviceConnection,
        protocol_name: str,
    ) -> ProtocolPort:
        """创建尚未建立连接的协议实例。"""
        ...
