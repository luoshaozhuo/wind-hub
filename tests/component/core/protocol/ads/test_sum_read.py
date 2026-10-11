"""ADS Index Sum Read 的请求顺序、逐点错误及传输失败测试。"""

from __future__ import annotations

import struct

import pytest

from core.application.errors import ProtocolError
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


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext={"index_group": 0x4020, "index_offset": offset, "data_type": "DINT"},
    )


def _driver(connection: object) -> ADSDriver:
    driver = ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable("ads", Protocol("ads"), {
            "a": _point("a", 12), "b": _point("b", 16),
        }),
        {},
    )
    driver._connection = connection
    driver._connected = True
    return driver


@pytest.mark.asyncio
async def test_sum_read_preserves_duplicates_and_individual_errors() -> None:
    class Connection:
        def read_write(
            self, group: int, count: int, _rd: object, request: object,
            _wd: object, *, check_length: bool,
        ) -> bytes:
            del group, _rd, _wd, check_length
            assert count == 3
            assert [(x.iOffset, x.size) for x in request] == [
                (16, 4), (12, 4), (12, 4),
            ]
            return struct.pack("<IIIiii", 0, 1808, 0, 2, 1, 3)

    samples = await _driver(Connection()).read_many(("b", "a", "a"))
    assert [(s.value, s.quality) for s in samples] == [
        (2, Quality.GOOD), (None, Quality.BAD), (3, Quality.GOOD),
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
    assert not driver.is_open()


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

    monkeypatch.setattr(driver_module, "pyads", _FakePyads)
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
