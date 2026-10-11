"""ADS index-addressed Sum Write regression tests."""

from __future__ import annotations

import struct

import pytest
from pyads.constants import ADSIGRP_SUMUP_WRITE

from core.application import ProtocolError, ProtocolWrite
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


def _point(point_id: str, offset: int, *, data_type: str = "DINT") -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext={"index_group": 0x4020, "index_offset": offset, "data_type": data_type},
    )


class _Connection:
    def __init__(self, errors: list[int] | None = None) -> None:
        self.errors = errors
        self.requests: list[list[tuple[int, int, bytes]]] = []
        self.closed = False

    def read_write(
        self,
        index_group: int,
        count: int,
        read_datatype: object,
        value: object,
        write_datatype: object,
        *,
        check_length: bool,
    ) -> bytes:
        assert index_group == ADSIGRP_SUMUP_WRITE
        assert read_datatype is None and write_datatype is None
        assert check_length is False
        raw = bytes(value)
        entries = [struct.unpack_from("<III", raw, i * 12) for i in range(count)]
        data_offset = 12 * count
        requests = []
        for group, offset, size in entries:
            requests.append((group, offset, raw[data_offset:data_offset + size]))
            data_offset += size
        assert data_offset == len(raw)
        self.requests.append(requests)
        errors = self.errors if self.errors is not None else [0] * count
        return b"".join(struct.pack("<I", x) for x in errors[:count])

    def close(self) -> None:
        self.closed = True


def _driver(*points: Point) -> tuple[ADSDriver, _Connection]:
    table = PointTable("ads", Protocol("ads"), {p.point_id: p for p in points})
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    connection = _Connection()
    driver._connection = connection
    driver._connected = True
    return driver, connection


@pytest.mark.asyncio
async def test_sum_write_uses_index_addresses_and_preserves_order() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [r.success for r in results] == [True, True]
    assert connection.requests == [[
        (0x4020, 10, struct.pack("<i", 1)),
        (0x4020, 20, struct.pack("<i", 2)),
    ]]


@pytest.mark.asyncio
async def test_sum_write_individual_status_isolated() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    connection.errors = [1808, 0]
    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [r.success for r in results] == [False, True]
    assert results[0].message == "ADS write error 1808"
    assert driver.is_open()


@pytest.mark.asyncio
async def test_sum_write_rejects_invalid_value_without_discarding_valid_points() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    results = await driver.write_many((
        ProtocolWrite("a", 1.5),
        ProtocolWrite("b", 2),
    ))
    assert [r.success for r in results] == [False, True]
    assert connection.requests == [[(0x4020, 20, struct.pack("<i", 2))]]


@pytest.mark.asyncio
async def test_sum_write_transport_failure_disconnects() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    connection.errors = [0]  # Invalid Sum Write response size
    with pytest.raises(ProtocolError, match="sum write failed"):
        await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert not driver.is_open()
    assert connection.closed


@pytest.mark.asyncio
async def test_sum_write_chunks_by_limit() -> None:
    from dataclasses import replace

    driver, connection = _driver(_point("a", 10), _point("b", 20), _point("c", 30))
    driver._config = replace(driver._config, max_subs_per_sum=2)
    results = await driver.write_many((
        ProtocolWrite("a", 1), ProtocolWrite("b", 2), ProtocolWrite("c", 3),
    ))
    assert all(r.success for r in results)
    assert [len(batch) for batch in connection.requests] == [2, 1]
