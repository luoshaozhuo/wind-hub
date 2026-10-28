"""IEC104 同一地址读取序列化与响应不串点验证。"""

from __future__ import annotations

import asyncio

import pytest

from tests.core.protocol.conftest import iec104_driver


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


@pytest.mark.asyncio
async def test_listen_routes_only_spontaneous_messages(iec104_server) -> None:
    """主动读取的请求响应不得被当作自发上送分发给 Listener。"""
    driver = iec104_driver(iec104_server.port)
    callbacks: list[tuple] = []

    async def on_samples(samples: tuple) -> None:
        callbacks.append(samples)

    await driver.connect()
    listener = await driver.listen(["power"], on_samples)
    try:
        await driver.read_one("power")
        await asyncio.sleep(0)
        assert callbacks == []
        await iec104_server.set_value(100, 1800.0, spontaneous=True)
        for _ in range(50):
            if callbacks:
                break
            await asyncio.sleep(0.02)
        assert callbacks
        assert callbacks[-1][0].value == pytest.approx(1800.0)
    finally:
        await listener.close()
        await driver.close()
