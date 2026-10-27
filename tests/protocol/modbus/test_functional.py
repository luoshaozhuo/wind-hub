"""Modbus 数据区映射、读取规划及纯计算编解码测试。"""

from __future__ import annotations

import math

import pytest

from core.application.errors import ConfigError
from core.domain import Point, PointAccess
from core.domain.identities import BusinessPointId
from core.infrastructure.protocol.modbus.codec import _encode_coil, _encode_registers, decode
from core.infrastructure.protocol.modbus.mapping import (
    group_consecutive_reads,
    parse_modbus_point,
)
from tests.support.units import NONE_UNIT


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ,
    address: int = 0,
    register_type: str = "holding",
    data_type: str = "uint16",
    slave_id: int = 1,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext={
            "address": address,
            "register_type": register_type,
            "data_type": data_type,
            "slave_id": slave_id,
        },
    )


def _mapped(point: Point):
    return parse_modbus_point(point, default_word_order="big_endian")


@pytest.mark.parametrize("kind", ["int16", "uint16", "int32", "uint32", "int64", "uint64"])
def test_integer_register_roundtrip(kind: str) -> None:
    value = -123 if kind.startswith("int") else 456
    words = _encode_registers(value, kind, "big_endian")
    mapped = _mapped(_point("p", data_type=kind))
    assert decode(mapped, words) == value


@pytest.mark.parametrize("kind", ["float32", "float64"])
@pytest.mark.parametrize("order", ["big_endian", "little_endian"])
def test_float_register_roundtrip(kind: str, order: str) -> None:
    words = _encode_registers(12.5, kind, order)
    point = _mapped(_point("p", data_type=kind))
    point = type(point)(
        point.point_id,
        point.register_type,
        point.address,
        point.count,
        point.data_type,
        order,
        point.slave_id,
    )
    assert math.isclose(decode(point, words), 12.5)


def test_coil_has_strict_boolean_semantics() -> None:
    assert _encode_coil(True, "bool") is True
    assert decode(_mapped(_point("p", register_type="coil", data_type="bool")), [1]) is True
    with pytest.raises(TypeError, match="bool"):
        _encode_coil(1, "bool")
    with pytest.raises(ValueError, match="bit"):
        decode(_mapped(_point("p", register_type="coil", data_type="bool")), [2])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_float_write_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _encode_registers(value, "float32", "big_endian")


def test_integer_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside range"):
        _encode_registers(65536, "uint16", "big_endian")


def test_read_planner_never_merges_distinct_areas_or_devices() -> None:
    points = [
        _mapped(_point("h1", address=10)),
        _mapped(_point("h2", address=12)),
        _mapped(_point("coil", address=11, register_type="coil", data_type="bool")),
        _mapped(_point("other_unit", address=11, slave_id=2)),
    ]
    groups = group_consecutive_reads(points)
    assert sorted(len(group) for group in groups) == [1, 1, 2]
    assert {point.point_id for group in groups for point in group} == {
        "h1",
        "h2",
        "coil",
        "other_unit",
    }


def test_read_planner_respects_span_and_gap() -> None:
    points = [
        _mapped(_point("a", address=1)),
        _mapped(_point("b", address=120)),
        _mapped(_point("c", address=126)),
    ]
    groups = group_consecutive_reads(points, max_gap=200)
    assert [[point.point_id for point in group] for group in groups] == [["a", "b"], ["c"]]


@pytest.mark.parametrize(
    ("address", "kind"),
    [(65535, "float32"), (-1, "uint16")],
)
def test_invalid_address_span_is_rejected(address: int, kind: str) -> None:
    with pytest.raises(ConfigError):
        _mapped(_point("p", address=address, data_type=kind))


def test_input_registers_are_never_writable() -> None:
    with pytest.raises(ConfigError, match="read-only"):
        _mapped(_point("p", access=PointAccess.WRITE, register_type="input"))


def test_decoding_requires_exact_register_count() -> None:
    with pytest.raises(ValueError, match="length"):
        decode(_mapped(_point("p", data_type="float32")), [0x3F80])
