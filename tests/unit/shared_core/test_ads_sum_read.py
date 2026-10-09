"""ADS Symbol 列表读取及 Index 路径隔离。"""

from __future__ import annotations

import pytest

from core.application.protocol_contract import Quality
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


class _Connection:
    def __init__(self) -> None:
        self.read_calls: list[list[str]] = []

    def read_list_by_name(
        self, names: list[str], *, ads_sub_commands: int
    ) -> dict[str, float]:
        assert ads_sub_commands > 0
        self.read_calls.append(list(names))
        return {"MAIN.a": 12.5, "MAIN.b": 2.0}


def _point(point_id: str, symbol: str) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext={"symbol": symbol, "data_type": "REAL"},
    )


@pytest.mark.asyncio
async def test_symbol_batch_uses_public_api_and_preserves_duplicates() -> None:
    points = {"a": _point("a", "MAIN.a"), "b": _point("b", "MAIN.b")}
    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable("ads", Protocol("ads"), points),
        {},
    )
    for key, point in driver._points.items():
        driver._points[key] = point.resolved(index_group=0x4020, index_offset=10)
    connection = _Connection()
    driver._connection = connection
    driver._connected = True

    result = await driver.read_many(("b", "a", "a"))
    assert [(sample.value, sample.quality) for sample in result] == [
        (2.0, Quality.GOOD),
        (12.5, Quality.GOOD),
        (12.5, Quality.GOOD),
    ]
    assert connection.read_calls == [["MAIN.b", "MAIN.a"]]


@pytest.mark.asyncio
async def test_symbol_read_error_text_is_bad_quality() -> None:
    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable(
            "ads",
            Protocol("ads"),
            {"a": _point("a", "MAIN.a"), "b": _point("b", "MAIN.b")},
        ),
        {},
    )
    for key, point in driver._points.items():
        driver._points[key] = point.resolved(index_group=0x4020, index_offset=10)

    class _ErrorConnection(_Connection):
        def read_list_by_name(
            self, names: list[str], *, ads_sub_commands: int
        ) -> dict[str, float | str]:
            del names, ads_sub_commands
            return {"MAIN.a": 12.5, "MAIN.b": "symbol not found"}

    driver._connection = _ErrorConnection()
    driver._connected = True
    samples = await driver.read_many(("a", "b"))
    assert [(sample.value, sample.quality) for sample in samples] == [
        (12.5, Quality.GOOD),
        (None, Quality.BAD),
    ]


@pytest.mark.asyncio
async def test_index_sum_read_falls_back_when_private_api_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.infrastructure.protocol.ads.driver as driver_module

    class _FakePyads:
        PLCTYPE_DINT = object()

    class _IndexConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def read(self, index_group: int, index_offset: int, datatype: object) -> int:
            del datatype
            self.calls.append((index_group, index_offset))
            return index_offset

    def _index_point(point_id: str, offset: int) -> Point:
        return Point(
            point_id=point_id,
            business_point_id=point_id,
            source_unit=UNIT_CATALOG[UnitCode.NONE],
            access=PointAccess.READ_WRITE,
            ext={
                "index_group": 0x4020,
                "index_offset": offset,
                "data_type": "DINT",
            },
        )

    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable(
            "ads",
            Protocol("ads"),
            {"a": _index_point("a", 12), "b": _index_point("b", 16)},
        ),
        {},
    )
    monkeypatch.setattr(driver_module, "_pyads", lambda: _FakePyads)
    connection = _IndexConnection()
    driver._connection = connection
    driver._connected = True

    def unavailable(addresses: object) -> bytes:
        del addresses
        raise NotImplementedError("internal handle unavailable")

    monkeypatch.setattr(driver, "_sum_read_bytes", unavailable)
    samples = await driver.read_many(("b", "a"))
    assert [(sample.value, sample.quality) for sample in samples] == [
        (16, Quality.GOOD),
        (12, Quality.GOOD),
    ]
    assert sorted(connection.calls) == [(0x4020, 12), (0x4020, 16)]


@pytest.mark.asyncio
async def test_index_sum_read_uses_public_read_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import struct

    from pyads.constants import ADSIGRP_SUMUP_READ

    class _ConnectionWithSum:
        def __init__(self) -> None:
            self.calls = 0

        def read_write(
            self,
            group: int,
            count: int,
            plc_read_datatype: object,
            request: object,
            plc_write_datatype: object,
            *,
            check_length: bool,
        ) -> bytes:
            self.calls += 1
            assert group == ADSIGRP_SUMUP_READ
            assert count == 2
            assert plc_read_datatype is None
            assert plc_write_datatype is None
            assert check_length is False
            assert ctypes.sizeof(request) == 24
            assert [(item.iGroup, item.iOffset, item.size) for item in request] == [
                (0x4020, 12, 4), (0x4020, 16, 4)
            ]
            return struct.pack("<IIII", 0, 0, 123, 456)

    class _FakePyads:
        PLCTYPE_DINT = ctypes.c_int32

    import core.infrastructure.protocol.ads.driver as driver_module

    monkeypatch.setattr(driver_module, "_pyads", lambda: _FakePyads)
    points = {}
    for name, offset in (("a", 12), ("b", 16)):
        points[name] = Point(
            point_id=name,
            business_point_id=name,
            source_unit=UNIT_CATALOG[UnitCode.NONE],
            access=PointAccess.READ_WRITE,
            ext={"index_group": 0x4020, "index_offset": offset, "data_type": "DINT"},
        )
    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable("ads", Protocol("ads"), points),
        {},
    )
    connection = _ConnectionWithSum()
    driver._connection = connection
    driver._connected = True
    samples = await driver.read_many(("a", "b"))
    assert [(sample.value, sample.quality) for sample in samples] == [
        (123, Quality.GOOD), (456, Quality.GOOD)
    ]
    assert connection.calls == 1
