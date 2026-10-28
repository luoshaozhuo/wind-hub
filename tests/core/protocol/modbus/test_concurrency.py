"""Modbus 真实会话并发与请求关联验证。"""

from __future__ import annotations

import asyncio

import pytest

from tests.core.protocol.conftest import modbus_driver


@pytest.mark.asyncio
async def test_concurrent_reads_do_not_cross_assign_points(modbus_server) -> None:
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        points = ["speed", "power", "temperature"] * 12
        result = await asyncio.gather(*(driver.read_one(point) for point in points))
        expected = {"speed": 1200.5, "power": 800.0, "temperature": 25}
        for point, sample in zip(points, result, strict=True):
            assert sample.point_id == point
            assert sample.value == pytest.approx(expected[point])
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_cancelled_modbus_caller_does_not_orphan_native_transaction() -> None:
    """取消调用方后，Session.close 必须等待已进入 PyModbus 的事务收敛。"""
    from typing import Any

    from core.infrastructure.protocol.modbus.config import ModbusConfig
    from core.infrastructure.protocol.modbus.session import _ModbusSession

    class FakeClient:
        connected = True

        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        async def read_holding_registers(self, address: int, *, count: int, device_id: int) -> Any:
            assert (address, count, device_id) == (100, 1, 1)
            self.entered.set()
            await self.release.wait()

            class Response:
                function_code = 3
                registers = [42]

                def isError(self) -> bool:  # noqa: N802
                    return False

            return Response()

        def close(self) -> None:
            self.closed = True
            self.connected = False

    client = FakeClient()
    session = _ModbusSession(ModbusConfig(host="127.0.0.1"))
    session._client = client  # type: ignore[assignment]
    waiting = asyncio.create_task(session.read("holding", 100, 1, 1))
    await asyncio.wait_for(client.entered.wait(), timeout=2.0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not closing.done()
    assert not client.closed
    client.release.set()
    await asyncio.wait_for(closing, timeout=2.0)
    assert client.closed
    assert session.closed
    assert not session._active
