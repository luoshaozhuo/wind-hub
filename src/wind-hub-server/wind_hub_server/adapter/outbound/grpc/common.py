"""Server gRPC client 公共生命周期辅助。"""

from __future__ import annotations

import grpc


class GrpcClientBase:
    """管理异步 gRPC channel，并统一保存默认 RPC deadline。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be > 0")
        self.target = target
        self.default_timeout = default_timeout
        self._channel = grpc.aio.insecure_channel(target)

    async def close(self) -> None:
        """关闭底层异步 gRPC channel。"""
        await self._channel.close()
