"""设备连接 Runtime。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from core.application.port import ProtocolPort
from core.domain import ConnectionId


class ConnectionRuntime:
    """ProtocolPort 实例及其连接生命周期的唯一 owner。"""

    def __init__(self, protocols: Mapping[ConnectionId, ProtocolPort]) -> None:
        self._protocols = dict(protocols)

    @property
    def protocols(self) -> Mapping[ConnectionId, ProtocolPort]:
        """返回只读协议实例索引。"""
        return MappingProxyType(self._protocols)

    def protocol(self, connection_id: ConnectionId) -> ProtocolPort:
        """按 connection_id 返回协议实例。"""
        return self._protocols[connection_id]

    async def start(self) -> None:
        """建立全部协议连接。

        当前采用 fail-fast 语义；重试、降级与 best-effort 属于后续运行策略，
        不在基础生命周期中隐式实现。
        """
        opened: list[ProtocolPort] = []
        try:
            for protocol in self._protocols.values():
                await protocol.connect()
                opened.append(protocol)
        except Exception:
            for protocol in reversed(opened):
                try:
                    await protocol.close()
                except Exception:
                    pass
            raise

    async def stop(self) -> None:
        """关闭全部协议实例；尽量完成所有资源释放。"""
        first_error: Exception | None = None
        for protocol in reversed(tuple(self._protocols.values())):
            try:
                await protocol.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
