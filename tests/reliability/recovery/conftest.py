"""Recovery 测试共享 fixture。

复用 system 层的 subprocess harness（``collector_factory`` / 空闲端口
``modbus_server``），并补一个空闲端口的 IEC104 fixture server——
两个 server fixture 都可反复起停，是协议故障注入的核心手段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.process import free_port

# 直接复用 system conftest 的 fixture 定义（import 即注册，不复制实现）。
from tests.system.conftest import collector_factory, modbus_server  # noqa: F401


@pytest.fixture
async def iec104_server() -> AsyncIterator[IEC104MockServer]:
    """独立空闲端口的真实 IEC104 从站（覆盖根 conftest 的固定端口版本）。"""
    server = IEC104MockServer(port=free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
