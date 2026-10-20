"""ADS index-addressed Sum Write regression tests."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest
from pyads.constants import (  # type: ignore[import-untyped]  # pyads 无 py.typed，与 src 处理方式一致
    ADSIGRP_SUMUP_WRITE,
)

import core.infrastructure.protocol.ads.driver as driver_module
from core.application import ProtocolError, ProtocolWrite
from core.application.protocol_contract import WriteOutcome
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


def _point(point_id: str, offset: int, *, data_type: str = "DINT") -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
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
        value: bytes,
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
            requests.append((group, offset, raw[data_offset : data_offset + size]))
            data_offset += size
        assert data_offset == len(raw)
        self.requests.append(requests)
        errors = self.errors if self.errors is not None else [0] * count
        return b"".join(struct.pack("<I", x) for x in errors[:count])

    def close(self) -> None:
        self.closed = True


def _driver(*points: Point) -> tuple[ADSDriver, _Connection]:
    table = PointTable(PointTableId("ads"), Protocol("ads"), {p.point_id: p for p in points})
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    connection = _Connection()
    driver._session = driver_module._AdsSession(connection)
    driver._connected = True
    return driver, connection


@pytest.mark.asyncio
async def test_sum_write_uses_index_addresses_and_preserves_order() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [r.outcome for r in results] == [
        WriteOutcome.CONFIRMED_SUCCESS,
        WriteOutcome.CONFIRMED_SUCCESS,
    ]
    assert connection.requests == [
        [
            (0x4020, 10, struct.pack("<i", 1)),
            (0x4020, 20, struct.pack("<i", 2)),
        ]
    ]


@pytest.mark.asyncio
async def test_sum_write_individual_status_isolated() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    connection.errors = [1808, 0]
    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [r.outcome for r in results] == [
        WriteOutcome.CONFIRMED_FAILURE,
        WriteOutcome.CONFIRMED_SUCCESS,
    ]
    assert results[0].message == "ADS write error 1808"
    assert driver.is_open()


@pytest.mark.asyncio
async def test_sum_write_rejects_invalid_value_without_discarding_valid_points() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    results = await driver.write_many(
        (
            ProtocolWrite("a", 1.5),
            ProtocolWrite("b", 2),
        )
    )
    assert [r.outcome for r in results] == [
        WriteOutcome.CONFIRMED_FAILURE,
        WriteOutcome.CONFIRMED_SUCCESS,
    ]
    assert connection.requests == [[(0x4020, 20, struct.pack("<i", 2))]]


@pytest.mark.asyncio
async def test_sum_write_transport_failure_keeps_session_open() -> None:
    """传输级失败（响应长度非法）逐点返回 UNKNOWN，但不擅自关闭会话。

    是否重连由上层 Watchdog 依据 health 决定；Driver 不把一次命令
    传输失败等同为 TCP 断链。
    """
    driver, connection = _driver(_point("a", 10), _point("b", 20))
    connection.errors = [0]  # 只返回 1 个错误码，响应长度非法
    results = await driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [result.outcome for result in results] == [WriteOutcome.UNKNOWN] * 2
    assert driver.is_open()
    assert not connection.closed


@pytest.mark.asyncio
async def test_sum_write_chunk_preserves_point_errors() -> None:
    """分批 Sum Write：逐点 ADS 错误码隔离，失败点不影响其他批次。"""
    driver, _unused = _driver(_point("a", 10), _point("b", 20), _point("c", 30))
    driver._config = replace(driver._config, max_subs_per_sum=2)

    class SelectiveErrors(_Connection):
        def read_write(
            self, index_group, count, read_datatype, value, write_datatype, *, check_length
        ) -> bytes:
            del read_datatype, write_datatype, check_length
            assert index_group == ADSIGRP_SUMUP_WRITE
            raw = bytes(value)
            offsets = [struct.unpack_from("<III", raw, i * 12)[1] for i in range(count)]
            return b"".join(struct.pack("<I", 1808 if offset == 20 else 0) for offset in offsets)

    connection = SelectiveErrors()
    driver._session = driver_module._AdsSession(connection)
    results = await driver.write_many(
        (
            ProtocolWrite("a", 1),
            ProtocolWrite("b", 2),
            ProtocolWrite("c", 3),
        )
    )
    assert [r.outcome for r in results] == [
        WriteOutcome.CONFIRMED_SUCCESS,
        WriteOutcome.CONFIRMED_FAILURE,
        WriteOutcome.CONFIRMED_SUCCESS,
    ]
    assert [r.message for r in results] == [None, "ADS write error 1808", None]
    assert connection.requests == []


@pytest.mark.asyncio
async def test_sum_write_chunks_by_limit() -> None:
    driver, connection = _driver(_point("a", 10), _point("b", 20), _point("c", 30))
    driver._config = replace(driver._config, max_subs_per_sum=2)
    results = await driver.write_many(
        (
            ProtocolWrite("a", 1),
            ProtocolWrite("b", 2),
            ProtocolWrite("c", 3),
        )
    )
    assert all(r.outcome is WriteOutcome.CONFIRMED_SUCCESS for r in results)
    assert [len(batch) for batch in connection.requests] == [2, 1]
