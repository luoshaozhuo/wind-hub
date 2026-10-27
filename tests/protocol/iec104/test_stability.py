"""IEC104 重复读稳定性；通过 PROTOCOL_STABILITY_ROUNDS 调节轮数。"""

from __future__ import annotations

import os

import pytest

from tests.protocol.conftest import iec104_driver


@pytest.mark.asyncio
async def test_iec104_repeated_reads_keep_correlated_values(iec104_server) -> None:
    rounds = int(os.environ.get("PROTOCOL_STABILITY_ROUNDS", "50"))
    assert rounds > 0
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    try:
        for _ in range(rounds):
            sample = await driver.read_one("power")
            assert sample.value == pytest.approx(1500.5)
            assert driver.is_open()
    finally:
        await driver.close()
