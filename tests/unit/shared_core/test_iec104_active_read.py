"""IEC104 主动读发送入口不返回旧镜像。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.application import ProtocolError, ProtocolSample
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.iec104.mapping import IEC104Point


class _Point:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls = 0

    def read(self) -> bool:
        self.calls += 1
        return self.accepted


class _Station:
    def __init__(self, point: _Point | None) -> None:
        self.point = point

    def get_point(self, ioa: int) -> _Point | None:
        assert ioa == 11
        return self.point


@pytest.mark.asyncio
async def test_active_read_sends_without_returning_cached_sample() -> None:
    driver = object.__new__(IEC104Driver)
    point = _Point()
    driver._is_open = True
    driver._station = _Station(point)
    driver._points_by_id = {"p": IEC104Point("p", 11, None)}
    driver._samples = {11: object()}
    assert await driver.request_read_one("p") is None
    assert point.calls == 1


@pytest.mark.asyncio
async def test_active_read_requires_registered_point() -> None:
    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._station = _Station(None)
    driver._points_by_id = {"p": IEC104Point("p", 11, None)}
    with pytest.raises(ProtocolError, match="not registered"):
        await driver.request_read_one("p")


@pytest.mark.asyncio
async def test_active_read_rejected_by_library() -> None:
    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._station = _Station(_Point(False))
    driver._points_by_id = {"p": IEC104Point("p", 11, None)}
    with pytest.raises(ProtocolError, match="rejected"):
        await driver.request_read_one("p")


@pytest.mark.asyncio
async def test_active_read_many_uses_ordered_individual_requests() -> None:
    driver = object.__new__(IEC104Driver)
    calls: list[str] = []

    async def fake_request(point_id: str) -> None:
        calls.append(point_id)

    driver.request_read_one = fake_request
    assert await driver.request_read_many(("b", "a", "b")) is None
    assert calls == ["b", "a", "b"]
    assert await driver.request_read_many(()) is None


@pytest.mark.asyncio
async def test_active_read_waits_for_matching_requested_response() -> None:
    import asyncio

    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._closed = False
    driver._points_by_id = {"p": IEC104Point("p", 11, None)}
    driver._points_by_ioa = {11: driver._points_by_id["p"]}
    driver._active_reads = {}
    driver._active_read_locks = {}
    driver._samples = {}

    class _Cfg:
        t1 = 0.2

    driver._config = _Cfg()

    async def send(_point_id: str) -> None:
        return None

    driver.request_read_one = send
    task = asyncio.create_task(driver.read_active_one("p"))
    await asyncio.sleep(0)
    sample = ProtocolSample(
        point_id="p",
        value=7,
        timestamp=datetime.now(UTC),
    )
    # A spontaneous notification must not resolve the explicit request.
    future = driver._active_reads[11]
    assert not future.done()
    future.set_result(sample)
    assert (await task).value == 7


@pytest.mark.asyncio
async def test_active_read_times_out_without_using_mirror() -> None:
    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._points_by_id = {"p": IEC104Point("p", 11, None)}
    driver._active_reads = {}
    driver._active_read_locks = {}

    class _Cfg:
        t1 = 0.001

    driver._config = _Cfg()

    async def send(_point_id: str) -> None:
        return None

    driver.request_read_one = send
    with pytest.raises(ProtocolError, match="timed out"):
        await driver.read_active_one("p")
    assert driver._active_reads == {}
