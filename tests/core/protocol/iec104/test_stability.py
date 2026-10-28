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


@pytest.mark.asyncio
async def test_iec104_long_soak_with_session_replacement(iec104_server) -> None:
    """循环关闭和重连，验证没有旧 Session 对新事务的污染。"""
    import asyncio
    import time

    duration = float(os.environ.get("PROTOCOL_STABILITY_SECONDS", "5"))
    assert duration > 0
    deadline = time.monotonic() + duration
    cycles = 0
    while time.monotonic() < deadline:
        driver = iec104_driver(iec104_server.port)
        await driver.connect()
        try:
            samples = await asyncio.gather(*(driver.read_one("power") for _ in range(4)))
            assert all(sample.value == pytest.approx(1500.5) for sample in samples)
        finally:
            await driver.close()
        assert not driver.is_open()
        cycles += 1
    assert cycles > 0
