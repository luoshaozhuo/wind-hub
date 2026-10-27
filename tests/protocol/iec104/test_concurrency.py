"""IEC104 同一地址读取序列化与响应不串点验证。"""

from __future__ import annotations

import asyncio

import pytest

from tests.protocol.conftest import iec104_driver


@pytest.mark.asyncio
async def test_same_ioa_parallel_reads_are_correlated(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    try:
        samples = await asyncio.gather(*(driver.read_one("power") for _ in range(8)))
        assert all(sample.point_id == "power" for sample in samples)
        assert all(sample.value == pytest.approx(1500.5) for sample in samples)
    finally:
        await driver.close()
