"""IEC104 单点读取只接受本次 C_RD_NA_1 响应。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.application import ProtocolError, ProtocolSample
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.iec104.mapping import IEC104Point


def _driver() -> IEC104Driver:
    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._closed = False
    driver._points_by_id = {"p": IEC104Point("p", 11, None)}
    driver._active_reads = {}
    driver._active_read_locks = {}

    class Config:
        t1 = 0.02

    driver._config = Config()
    return driver


@pytest.mark.asyncio
async def test_read_one_waits_for_requested_response_not_mirror() -> None:
    driver = _driver()
    calls = []

    async def send(mapped: IEC104Point) -> None:
        calls.append(mapped.ioa)

    driver._send_read = send
    task = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0)
    assert calls == [11]
    assert not task.done()
    driver._active_reads[11].set_result(
        ProtocolSample(point_id="p", value=7, timestamp=datetime.now(UTC))
    )
    assert (await task).value == 7


@pytest.mark.asyncio
async def test_read_one_times_out_without_response() -> None:
    driver = _driver()

    async def send(mapped: IEC104Point) -> None:
        del mapped

    driver._send_read = send
    with pytest.raises(ProtocolError, match="timed out"):
        await driver.read_one("p")
    assert not driver._active_reads


@pytest.mark.asyncio
async def test_read_one_serializes_same_ioa() -> None:
    driver = _driver()
    sent = []

    async def send(mapped: IEC104Point) -> None:
        sent.append(mapped.ioa)

    driver._send_read = send
    first = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0)
    second = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0)
    assert sent == [11]
    driver._active_reads[11].set_result(
        ProtocolSample(point_id="p", value=1, timestamp=datetime.now(UTC))
    )
    await first
    await asyncio.sleep(0)
    assert sent == [11, 11]
    driver._active_reads[11].set_result(
        ProtocolSample(point_id="p", value=2, timestamp=datetime.now(UTC))
    )
    assert (await second).value == 2
