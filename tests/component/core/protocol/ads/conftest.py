"""ADS 组件测试的 driver 模块全局态隔离。

本地 AMS 身份、PLC Net ID 占用表与模块级 asyncio.Lock 是进程级单例
（生产进程只有一个事件循环）。pytest-asyncio 每个用例使用独立事件循环，
锁会在首次使用时绑定到当前循环，因此必须逐用例重建并清空占用表。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
from core.application.settings import ADSLocalConfig


class FakeNotificationAttrib:
    """NotificationAttrib 替身：仅记录注册参数。"""

    def __init__(self, size: int, **kwargs: Any) -> None:
        self.size = size
        self.kwargs = kwargs


class FakePyadsModule:
    """driver/subscription 模块内 ``pyads`` 名字的替身。"""

    def __init__(self) -> None:
        self.opened_ports = 0
        self.closed_ports = 0
        self.local_address: str | None = None
        self.route_calls: list[tuple[Any, ...]] = []
        self.route_result = True
        self.route_error: BaseException | None = None
        self.ADSSTATE_RUN = 5
        self.ADSTRANS_SERVERCYCLE = 3
        self.NotificationAttrib = FakeNotificationAttrib

    def open_port(self) -> int:
        self.opened_ports += 1
        return 42

    def close_port(self) -> None:
        self.closed_ports += 1

    def set_local_address(self, net_id: str) -> None:
        self.local_address = net_id

    def add_route_to_plc(self, *args: Any) -> bool:
        self.route_calls.append(args)
        if self.route_error is not None:
            raise self.route_error
        return self.route_result


@pytest.fixture(autouse=True)
def _reset_ads_driver_globals() -> Any:
    driver_module._LOCAL_IDENTITY = None
    driver_module._LOCAL_PORT_OPEN = False
    driver_module._LOCAL_CLOSED = False
    driver_module._TARGET_OWNERS.clear()
    driver_module._LOCAL_LOCK = asyncio.Lock()
    driver_module._REMOTE_ROUTE_LOCK = asyncio.Lock()
    yield
    driver_module._LOCAL_IDENTITY = None
    driver_module._LOCAL_PORT_OPEN = False
    driver_module._LOCAL_CLOSED = False
    driver_module._TARGET_OWNERS.clear()
    driver_module._LOCAL_LOCK = asyncio.Lock()
    driver_module._REMOTE_ROUTE_LOCK = asyncio.Lock()


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> FakePyadsModule:
    import core.infrastructure.protocol.ads.subscription as subscription_module

    fake = FakePyadsModule()
    monkeypatch.setattr(driver_module, "pyads", fake)
    monkeypatch.setattr(subscription_module, "pyads", fake)
    return fake


@pytest.fixture
async def ads_identity(fake_pyads: FakePyadsModule) -> ADSLocalConfig:
    """initialize_local 的已就绪本机 AMS 身份（进程级 API 全部替身化）。"""
    identity = ADSLocalConfig(
        local_ams_net_id="192.0.2.1.1.1",
        local_ip="192.0.2.1",
        request_timeout_ms=1000,
        connect_timeout_ms=3000,
        close_timeout_ms=3000,
    )
    await driver_module.ADSDriver.initialize_local(identity)
    return identity
