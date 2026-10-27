"""ADS Subscription 的确定性故障注入与资源释放验收。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from ctypes import c_ubyte
from typing import Any

import pytest

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.ads import ADSDriver
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.ads.subscription import ADSSubscription
from tests.protocol.conftest import make_point, make_table


class FakeAdsSession:
    """仅模拟 ADS 原生调用的可观测完成，不冒充网络协议。"""

    def __init__(self) -> None:
        self.operations: list[str] = []
        self.notification_registered = asyncio.Event()
        self.allow_notification_finish = asyncio.Event()
        self.token: tuple[int, int] = (17, 0)

    def notification(self, datatype: object) -> Callable[[Callable[..., None]], Callable[..., None]]:
        assert datatype == c_ubyte * 2

        def decorate(callback: Callable[..., None]) -> Callable[..., None]:
            return callback

        return decorate

    async def execute(self, operation: str, **kwargs: Any) -> tuple[int, int] | None:
        self.operations.append(operation)
        if operation == "add_device_notification":
            self.notification_registered.set()
            await self.allow_notification_finish.wait()
            return self.token
        if operation == "del_device_notification":
            assert kwargs["notification_handle"] == self.token[0]
            assert kwargs["user_handle"] == self.token[1]
            return None
        raise AssertionError(f"Unexpected ADS operation: {operation}")


def _subscription(session: FakeAdsSession) -> ADSSubscription:
    mapped = ADSPoint(
        point_id="p",
        index_group=0x4020,
        index_offset=100,
        data_type="INT",
        size=2,
        symbol="MAIN.p",
    )

    async def callback(samples: object) -> None:
        del samples

    return ADSSubscription(
        session,  # type: ignore[arg-type]  # Native I/O is isolated by the fake
        (mapped,),
        callback,
        interval=0.1,
        max_block_bytes=20,
        max_delay=0.01,
    )


@pytest.mark.asyncio
async def test_subscription_start_and_stop_release_registered_handle() -> None:
    session = FakeAdsSession()
    subscription = _subscription(session)
    session.allow_notification_finish.set()
    await subscription.start()
    await subscription.stop()
    assert session.operations == ["add_device_notification", "del_device_notification"]


@pytest.mark.asyncio
async def test_cancelled_start_does_not_lose_late_registration() -> None:
    session = FakeAdsSession()
    subscription = _subscription(session)
    starting = asyncio.create_task(subscription.start())
    await session.notification_registered.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    session.allow_notification_finish.set()
    await subscription.stop()
    assert session.operations == ["add_device_notification", "del_device_notification"]


def test_invalid_ads_point_is_rejected_without_network() -> None:
    with pytest.raises(ConfigError, match="size"):
        ADSDriver(
            ConnectionEndpoint("127.0.0.1", 851),
            make_table(
                "ads",
                make_point("bad", symbol="MAIN.bad", data_type="REAL", size=3),
            ),
            {"target_net_id": "1.2.3.4.5.6"},
        )
