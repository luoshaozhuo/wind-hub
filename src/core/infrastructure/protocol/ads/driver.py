"""ADS 协议驱动：第三轮实现。

Driver 管理协议策略及五态生命周期；Session 管理唯一 Connection。
第一轮只保留公开入口，禁止假装能够正常通信。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)
from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .config import ADSConfig, parse_ads_config
from .subscription import ADSSubscription


class ADSDriver:
    """单一 PLC 的 ADS 协议适配器。"""

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        self._config: ADSConfig = parse_ads_config(endpoint, protocol_options)
        self._point_table = point_table

    @classmethod
    async def initialize_local(cls, config: ADSLocalConfig) -> None:
        raise NotImplementedError("AMS lifecycle is scheduled for round three")

    @classmethod
    async def close_local(cls) -> None:
        raise NotImplementedError("AMS lifecycle is scheduled for round three")

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
                ProtocolCapability.SUBSCRIBE,
            }
        )

    def update_point_table(self, point_table: PointTable) -> None:
        self._point_table = point_table

    async def connect(self) -> None:
        raise NotImplementedError("ADS driver is scheduled for round three")

    async def close(self) -> None:
        raise NotImplementedError("ADS driver is scheduled for round three")

    def is_open(self) -> bool:
        return False

    async def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=False, message="ADS driver not implemented")

    async def read_one(self, point_id: str) -> ProtocolSample:
        raise NotImplementedError("ADS read is scheduled for round three")

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        raise NotImplementedError("ADS read is scheduled for round three")

    async def write_one(self, write: ProtocolWrite[Any]) -> ProtocolWriteResult:
        raise NotImplementedError("ADS write is scheduled for round three")

    async def write_many(
        self, writes: Sequence[ProtocolWrite[Any]]
    ) -> tuple[ProtocolWriteResult, ...]:
        raise NotImplementedError("ADS write is scheduled for round three")

    def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> ADSSubscription:
        raise NotImplementedError("ADS subscribe is scheduled for round three")
