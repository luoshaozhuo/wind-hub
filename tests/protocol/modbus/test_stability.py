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
