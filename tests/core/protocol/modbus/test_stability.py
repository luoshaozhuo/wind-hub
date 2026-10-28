"""Modbus 重复事务稳定性；通过 PROTOCOL_STABILITY_ROUNDS 调节轮数。"""

from __future__ import annotations

import os

import pytest

from tests.protocol.conftest import modbus_driver


@pytest.mark.asyncio
async def test_modbus_repeated_reads_keep_values_and_connection(modbus_server) -> None:
    rounds = int(os.environ.get("PROTOCOL_STABILITY_ROUNDS", "100"))
    assert rounds > 0
    driver = modbus_driver(modbus_server.port)
    await driver.connect()
    try:
        for _ in range(rounds):
            sample = await driver.read_one("speed")
            assert sample.value == pytest.approx(1200.5)
            assert driver.is_open()
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_modbus_long_soak_with_repeated_reconnect(modbus_server) -> None:
    """持续读与定期主动重连；环境变量控制长时间测试时长。"""
    import asyncio
    import time

    duration = float(os.environ.get("PROTOCOL_STABILITY_SECONDS", "5"))
    assert duration > 0
    driver = modbus_driver(modbus_server.port)
    deadline = time.monotonic() + duration
    completed = 0
    while time.monotonic() < deadline:
        await driver.connect()
        try:
            for _ in range(20):
                samples = await driver.read_many(["speed", "power"])
                assert [sample.value for sample in samples] == pytest.approx([1200.5, 800.0])
                completed += 1
        finally:
            await driver.close()
        assert not driver.is_open()
        await asyncio.sleep(0)
    assert completed >= 20
