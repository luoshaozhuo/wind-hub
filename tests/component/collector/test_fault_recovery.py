"""端到端测试 —— 故障恢复：从站宕机后驱动报告不健康，恢复后自动重连。"""

from __future__ import annotations

import asyncio

from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub_collector.assembly import CollectorApp


async def _wait_until(coro_factory, timeout: float = 15.0) -> None:
    """轮询等待一个布尔条件为真，超时则失败。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await coro_factory():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not met within {timeout}s")


def _modbus_healthy(runtime: CollectorApp, expected: bool):
    async def _check() -> bool:
        return runtime.runtime.devices["modbus-1"].protocol.health().healthy is expected

    return _check


async def test_fault_recovery_reconnects(
    runtime: CollectorApp,
    modbus_server: ModbusMockServer,
) -> None:
    """从站宕机 → 不健康；从站恢复 → 驱动自动重连恢复健康。"""
    # 初始已连接
    await _wait_until(_modbus_healthy(runtime, True))

    # 模拟从站故障
    await modbus_server.stop()
    await _wait_until(_modbus_healthy(runtime, False))

    # 从站恢复
    await modbus_server.start()
    await _wait_until(_modbus_healthy(runtime, True))
