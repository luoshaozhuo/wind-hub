"""ADS 地址映射、标量编解码及 Notification 规划测试。"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from core.application.errors import ConfigError
from core.domain import Point, PointAccess
from core.domain.identities import BusinessPointId
from core.infrastructure.protocol.ads.codec import (
    _coerce_write_value,
    _decode_sum_write_errors,
    _encode_value,
    compile_notification_blocks,
    encode_sum_write,
)
from core.infrastructure.protocol.ads.mapping import parse_ads_point, symbol_index_address
from tests.support.units import NONE_UNIT


def _point(point_id: str, **ext: str | int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext=ext,
    )


def _ads(point_id: str, offset: int, kind: str = "uint16"):
    return parse_ads_point(
        _point(
            point_id,
            symbol=f"MAIN.{point_id}",
            index_group=0x4020,
            index_offset=offset,
            data_type=kind,
        )
    )


@pytest.mark.parametrize(
    ("kind", "value", "encoded"),
    [
        ("BOOL", True, b"\x01"),
        ("INT", -123, struct.pack("<h", -123)),
        ("UDINT", 123456, struct.pack("<I", 123456)),
        ("REAL", 1.5, struct.pack("<f", 1.5)),
    ],
)
def test_scalar_encode_decode(kind: str, value: bool | int | float, encoded: bytes) -> None:
    point = parse_ads_point(_point("value", symbol="MAIN.value", data_type=kind))
    assert _encode_value(value, point) == encoded
    assert point.decoder(b"\xff" + encoded, 1) == value


def test_symbol_address_is_unresolved_until_bound() -> None:
    point = parse_ads_point(_point("p", symbol="MAIN.p", data_type="uint16"))
    assert not point.address_resolved
    with pytest.raises(ConfigError, match="unresolved"):
        _ = point.group

    bound = point.resolved(index_group=0x4020, index_offset=17)
    assert bound.address_resolved
    assert (bound.group, bound.offset) == (0x4020, 17)
    assert not bound.unresolved().address_resolved


def test_sum_write_payload_contains_address_headers_then_values() -> None:
    first = _ads("a", 10)
    second = _ads("b", 20)
    body = encode_sum_write(
        [(0, first, b"\x01\x00"), (1, second, b"\x02\x00")]
    )
    assert bytes(body) == (
        struct.pack("<III", 0x4020, 10, 2)
        + struct.pack("<III", 0x4020, 20, 2)
        + b"\x01\x00\x02\x00"
    )


def test_notification_blocks_use_relative_offsets_and_split_by_capacity() -> None:
    blocks = compile_notification_blocks(
        [_ads("c", 30), _ads("a", 10), _ads("b", 12)], max_bytes=8
    )
    assert [(block.offset, block.size) for block in blocks] == [(10, 4), (30, 2)]
    assert [(point.point_id, offset) for point, offset, _ in blocks[0].fields] == [
        ("a", 0),
        ("b", 2),
    ]


def test_notification_overlap_is_rejected() -> None:
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks([_ads("a", 10, "uint32"), _ads("b", 12, "uint32")])


@pytest.mark.parametrize("bad", [-1, 256])
def test_unsigned_write_range_is_checked(bad: int) -> None:
    with pytest.raises(ValueError):
        _coerce_write_value(bad, "USINT")


def test_ads_rejects_incomplete_index_pair_and_invalid_symbol_reply() -> None:
    with pytest.raises(ConfigError, match="together"):
        parse_ads_point(_point("p", symbol="MAIN.p", data_type="INT", index_group=10))
    with pytest.raises(ConfigError, match="invalid index"):
        symbol_index_address("MAIN.p", SimpleNamespace(index_group=True, index_offset=1))


def test_sum_write_error_vector_is_exact_length() -> None:
    assert _decode_sum_write_errors(struct.pack("<II", 0, 1793), 2) == (0, 1793)
    with pytest.raises(Exception, match="size"):
        _decode_sum_write_errors(struct.pack("<I", 0), 2)
