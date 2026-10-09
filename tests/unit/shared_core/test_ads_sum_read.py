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

    result = await driver.read_raw(("b", "a", "a"))
    assert result == (
        (2.0, Quality.GOOD),
        (12.5, Quality.GOOD),
        (12.5, Quality.GOOD),
    )
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
    assert await driver.read_raw(("a", "b")) == (
        (12.5, Quality.GOOD),
        (None, Quality.BAD),
    )
