"""Recovery 测试共享 fixture。

复用 system 层的 subprocess harness（``collector_factory`` / 空闲端口
``modbus_server``），并补一个空闲端口的 IEC104 fixture server——
两个 server fixture 都可反复起停，是协议故障注入的核心手段。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import core.infrastructure.protocol.ads.driver as _ads_driver_module  # noqa: E402

# ADS：真实 AdsTestServer fixture + component 层的替身 pyads/身份 fixture。
from tests.component.core.protocol.ads.conftest import (  # noqa: F401,E402
    ads_identity,
    fake_pyads,
)
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.integration.protocols.ads.conftest import ads_service  # noqa: F401,E402
from tests.support.process import free_port

# 直接复用 system conftest 的 fixture 定义（import 即注册，不复制实现）。
from tests.support.worker_fixtures import collector_factory  # noqa: F401
from tests.system.conftest import modbus_server  # noqa: F401


@pytest.fixture(autouse=True)
def _ads_globals_isolated() -> Any:
    """逐用例隔离 ADS 模块级单例（事件循环逐用例重建，锁必须重建）。"""
    _ads_driver_module._LOCAL_IDENTITY = None
    _ads_driver_module._LOCAL_PORT_OPEN = False
    _ads_driver_module._LOCAL_CLOSED = False
    _ads_driver_module._TARGET_OWNERS.clear()
    _ads_driver_module._LOCAL_LOCK = asyncio.Lock()
    _ads_driver_module._REMOTE_ROUTE_LOCK = asyncio.Lock()
    yield
    _ads_driver_module._LOCAL_IDENTITY = None
    _ads_driver_module._LOCAL_PORT_OPEN = False
    _ads_driver_module._LOCAL_CLOSED = False
    _ads_driver_module._TARGET_OWNERS.clear()
    _ads_driver_module._LOCAL_LOCK = asyncio.Lock()
    _ads_driver_module._REMOTE_ROUTE_LOCK = asyncio.Lock()


@pytest.fixture
async def iec104_server() -> AsyncIterator[IEC104MockServer]:
    """独立空闲端口的真实 IEC104 从站（覆盖根 conftest 的固定端口版本）。"""
    server = IEC104MockServer(port=free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
