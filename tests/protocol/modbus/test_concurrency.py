"""Modbus 真实会话并发与请求关联验证。"""

from __future__ import annotations

import asyncio

import pytest

from tests.protocol.conftest import modbus_driver


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
