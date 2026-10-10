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


@pytest.mark.asyncio
async def test_overlapping_symbol_spans_fall_back_to_ordered_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver_module, "_pyads", lambda: _FakePyads)
    driver = _driver(_point("a", symbol="MAIN.a"), _point("b", symbol="MAIN.b"))
    driver._points["a"] = driver._points["a"].resolved(
        index_group=0x4020, index_offset=100
    )
    driver._points["b"] = driver._points["b"].resolved(
        index_group=0x4020, index_offset=102
    )
    result = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [entry.success for entry in result] == [True, True]
    assert driver._connection.batch == []
    assert [entry[1] for entry in driver._connection.individual] == [100, 102]


class _FakeADSError(Exception):
    def __init__(self, message: str, err_code: int) -> None:
        super().__init__(message)
        self.err_code = err_code


class _PointFailingConnection(_FakeConnection):
    """第一个点抛点级 ADS 错误（symbol 不存在），其余正常。"""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error
        self._raised = False

    def write(
        self, index_group: int, index_offset: int, value: object, datatype: object
    ) -> None:
        if not self._raised:
            self._raised = True
            raise self._error
        super().write(index_group, index_offset, value, datatype)


@pytest.mark.asyncio
async def test_point_level_write_error_isolated_without_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点级 ADS 错误（symbol 被 PLC 删除）只收敛为单点失败，不标记断线。"""
    fake_pyads = type("_FakePyads", (), {"PLCTYPE_DINT": object(), "ADSError": _FakeADSError})
    monkeypatch.setattr(driver_module, "_pyads", lambda: fake_pyads)
    driver = _driver(_point("a"), _point("b"))
    driver._connection = _PointFailingConnection(_FakeADSError("symbol gone", 1808))

    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))

    assert [r.success for r in results] == [False, True]
    assert driver.health().healthy is True


@pytest.mark.asyncio
async def test_transport_write_error_marks_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接/传输级写错误统一标记断线并抛 ProtocolError。"""
    fake_pyads = type("_FakePyads", (), {"PLCTYPE_DINT": object(), "ADSError": _FakeADSError})
    monkeypatch.setattr(driver_module, "_pyads", lambda: fake_pyads)
    driver = _driver(_point("a"), _point("b"))
    driver._connection = _PointFailingConnection(_FakeADSError("port closed", 1861))

    with pytest.raises(Exception, match="write failed"):
        await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert driver.health().healthy is False
