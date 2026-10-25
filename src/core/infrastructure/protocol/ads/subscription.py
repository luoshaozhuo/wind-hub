"""ADS 原生通知订阅：第三轮实现。"""

from __future__ import annotations


class ADSSubscription:
    """一次性通知句柄；start/stop 是资源生命周期入口。"""

    async def start(self) -> None:
        raise NotImplementedError("ADS subscription is scheduled for round three")

    async def stop(self) -> None:
        raise NotImplementedError("ADS subscription is scheduled for round three")
