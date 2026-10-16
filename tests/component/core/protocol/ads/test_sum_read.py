"""ADS Index Sum Read 的请求顺序、逐点错误及传输失败测试。"""

from __future__ import annotations

import struct
from typing import Any

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import Quality
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.units import NONE_UNIT


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"index_group": 0x4020, "index_offset": offset, "data_type": "DINT"},
    )


def _driver(connection: object) -> ADSDriver:
    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable(
            PointTableId("ads"),
            Protocol("ads"),
            {
                "a": _point("a", 12),
                "b": _point("b", 16),
            },
        ),
        {},
    )
    driver._connection = connection
    driver._connected = True
    return driver


@pytest.mark.asyncio
async def test_sum_read_preserves_duplicates_and_individual_errors() -> None:
    class Connection:
        def read_write(
            self,
            group: int,
            count: int,
            _rd: object,
            request: Any,
            _wd: object,
            *,
            check_length: bool,
        ) -> bytes:
            del group, _rd, _wd, check_length
            assert count == 3
            assert [(x.iOffset, x.size) for x in request] == [
                (16, 4),
                (12, 4),
                (12, 4),
            ]
            return struct.pack("<IIIiii", 0, 1808, 0, 2, 1, 3)

    samples = await _driver(Connection()).read_many(("b", "a", "a"))
    assert [(s.value, s.quality) for s in samples] == [
        (2, Quality.GOOD),
        (None, Quality.BAD),
        (3, Quality.GOOD),
    ]


@pytest.mark.asyncio
async def test_sum_read_transport_failure_never_falls_back_to_sequential() -> None:
    class Connection:
        def read_write(self, *_args: object, **_kwargs: object) -> bytes:
            raise ConnectionError("ADS communication lost")

        def close(self) -> None:
            pass

    driver = _driver(Connection())
    with pytest.raises(ProtocolError, match="ADS read failed"):
        await driver.read_many(("a", "b"))
    assert driver.is_open()  # 一次通信失败不自动销毁会话，由 Watchdog 判定恢复


@pytest.mark.asyncio
async def test_index_sum_read_uses_public_read_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    import struct

    from pyads.constants import (  # type: ignore[import-untyped]  # pyads 无 py.typed，与 src 处理方式一致
        ADSIGRP_SUMUP_READ,
    )

    class _ConnectionWithSum:
        def __init__(self) -> None:
            self.calls = 0

        def read_write(
            self,
            group: int,
            count: int,
            plc_read_datatype: object,
            request: Any,
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
                (0x4020, 12, 4),
                (0x4020, 16, 4),
            ]
            return struct.pack("<IIII", 0, 0, 123, 456)

    class _FakePyads:
        PLCTYPE_DINT = ctypes.c_int32

    import core.infrastructure.protocol.ads.driver as driver_module

    monkeypatch.setattr(driver_module, "pyads", _FakePyads)
    points = {}
    for name, offset in (("a", 12), ("b", 16)):
        points[name] = Point(
            point_id=name,
            business_point_id=BusinessPointId(name),
            source_unit=NONE_UNIT,
            access=PointAccess.READ_WRITE,
            ext={"index_group": 0x4020, "index_offset": offset, "data_type": "DINT"},
        )
    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable(PointTableId("ads"), Protocol("ads"), points),
        {},
    )
    connection = _ConnectionWithSum()
    driver._connection = connection
    driver._connected = True
    samples = await driver.read_many(("a", "b"))
    assert [(sample.value, sample.quality) for sample in samples] == [
        (123, Quality.GOOD),
        (456, Quality.GOOD),
    ]
    assert connection.calls == 1


def _symbol_point(point_id: str, symbol: str, data_type: str, size: int | None = None) -> Point:
    ext: dict[str, str | int] = {"symbol": symbol, "data_type": data_type}
    if size is not None:
        ext["size"] = size
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext=ext,
    )


@pytest.mark.asyncio
async def test_symbol_cache_shared_by_multiple_configured_points() -> None:
    """同一符号被多个配置点引用时，一个 session 内只查询一次符号。"""
    from types import SimpleNamespace

    class Connection:
        def __init__(self) -> None:
            self.calls = 0

        def get_symbol(self, symbol: str) -> object:
            assert symbol == "MAIN.power"
            self.calls += 1
            return SimpleNamespace(index_group=0x4020, index_offset=120, size=4)

    table = PointTable(
        PointTableId("ads"),
        Protocol("ads"),
        {
            "a": _symbol_point("a", "MAIN.power", "DINT"),
            "b": _symbol_point("b", "MAIN.power", "DINT"),
        },
    )
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    connection = Connection()
    driver._connection = connection

    await driver._resolve_configured_points()

    assert connection.calls == 1
    assert driver._points["a"].address_resolved
    assert (driver._points["a"].group, driver._points["a"].offset) == (0x4020, 120)
    assert (driver._points["b"].group, driver._points["b"].offset) == (0x4020, 120)


@pytest.mark.asyncio
async def test_symbol_size_rules_and_point_resolution_failure() -> None:
    """STRING 保持配置长度；定长标量与 PLC 符号宽度不符时拒绝建链。"""
    from types import SimpleNamespace

    from core.application.errors import ConfigError

    class Connection:
        def get_symbol(self, symbol: str) -> object:
            offset = 12 if symbol == "MAIN.name" else 20
            return SimpleNamespace(index_group=0x4020, index_offset=offset, size=8)

    table = PointTable(
        PointTableId("ads"),
        Protocol("ads"),
        {
            "name": _symbol_point("name", "MAIN.name", "STRING", 81),
            "power": _symbol_point("power", "MAIN.power", "DINT"),
        },
    )
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    driver._connection = Connection()

    with pytest.raises(ConfigError, match="PLC size"):
        await driver._resolve_configured_points()

    # 仅 STRING 点时宽度不参与校验，保持配置长度。
    string_table = PointTable(
        PointTableId("ads2"),
        Protocol("ads"),
        {"name": _symbol_point("name", "MAIN.name", "STRING", 81)},
    )
    string_driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), string_table, {})
    string_driver._connection = Connection()
    await string_driver._resolve_configured_points()
    assert string_driver._points["name"].size == 81
