"""ADS 公开 Sum Write 执行及 Index 回退测试。"""

from __future__ import annotations

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
from core.application import ProtocolWrite
from core.domain import (
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.ads import ADSDriver


def _point(point_id: str, *, symbol: str | None = None) -> Point:
    ext: dict[str, object] = {"data_type": "DINT"}
    if symbol is None:
        ext.update(index_group=0x4020, index_offset=10)
    else:
        ext["symbol"] = symbol
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext=ext,
    )


class _FakeConnection:
    def __init__(self) -> None:
        self.batch: list[dict[str, object]] = []
        self.individual: list[tuple[int, int, object]] = []

    def write_list_by_name(
        self, values: dict[str, object], *, ads_sub_commands: int
    ) -> dict[str, str]:
        assert ads_sub_commands > 0
        self.batch.append(dict(values))
        return {symbol: "no error" if symbol == "MAIN.a" else "device rejected"
                for symbol in values}

    def write(
        self, index_group: int, index_offset: int, value: object, datatype: object
    ) -> None:
        del datatype
        self.individual.append((index_group, index_offset, value))


class _FakePyads:
    PLCTYPE_DINT = object()


def _driver(*points: Point) -> ADSDriver:
    table = PointTable("ads", Protocol("ads"), {p.point_id: p for p in points})
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    driver._connection = _FakeConnection()
    driver._connected = True
    return driver


@pytest.mark.asyncio
async def test_unique_symbols_use_public_sum_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver_module, "_pyads", lambda: _FakePyads)
    driver = _driver(_point("a", symbol="MAIN.a"), _point("b", symbol="MAIN.b"))
    # Normally performed during connect: bind symbol addresses to this session.
    for index, (key, point) in enumerate(driver._points.items()):
        driver._points[key] = point.resolved(
            index_group=0x4020, index_offset=10 + index * 4
        )

    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [result.success for result in results] == [True, False]
    assert driver._connection.batch == [{"MAIN.a": 1, "MAIN.b": 2}]
    assert driver._connection.individual == []


@pytest.mark.asyncio
async def test_index_writes_remain_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver_module, "_pyads", lambda: _FakePyads)
    driver = _driver(_point("a"), _point("b"))
    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert all(result.success for result in results)
    assert driver._connection.batch == []
    assert len(driver._connection.individual) == 2


@pytest.mark.asyncio
async def test_distinct_symbols_same_index_address_do_not_sum_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver_module, "_pyads", lambda: _FakePyads)
    driver = _driver(_point("a", symbol="MAIN.a"), _point("b", symbol="MAIN.b"))
    for key, point in driver._points.items():
        driver._points[key] = point.resolved(index_group=0x4020, index_offset=10)

    result = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert all(entry.success for entry in result)
    assert driver._connection.batch == []
    assert len(driver._connection.individual) == 2
