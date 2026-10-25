"""ADS 单次连接会话：第二轮实现。

会话独占一个 pyads.Connection；同步 ADS 命令在同一会话内串行执行。
连接线程未实际返回前，不得释放资源所有权。
"""

from __future__ import annotations

from typing import Any


class _AdsSession:
    """一次性 ADS 连接的资源所有者；当前仅定义下一轮的接口。"""

    async def open(self) -> None:
        raise NotImplementedError("ADS session is scheduled for round two")

    async def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ADS session is scheduled for round two")

    async def close(self) -> None:
        raise NotImplementedError("ADS session is scheduled for round two")
