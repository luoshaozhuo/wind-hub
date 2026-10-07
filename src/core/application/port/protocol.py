"""共享设备协议 outbound port。

这里只定义 Collector 与 Commander 都需要的最小设备通信接口。
协议订阅、总召等应用专有能力不在 Shared Core Application Port 中定义。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..protocol_contract import (
    ConnectionHealth,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)


class ProtocolPort(Protocol):
    """共享的最小设备通信能力边界。"""

    async def connect(self) -> None:
        ...

    async def close(self) -> None:
        ...

    def health(self) -> ConnectionHealth:
        ...

    async def read(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        ...

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        ...
