"""IEC104 主动读发送入口不返回旧镜像。"""

from __future__ import annotations

import pytest

from core.application import ProtocolError
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
