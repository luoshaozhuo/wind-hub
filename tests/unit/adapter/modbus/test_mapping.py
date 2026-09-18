"""Unit tests for Modbus point mapping and read grouping."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.modbus.mapping import (
    ModbusPoint,
    group_consecutive_reads,
    parse_point,
)
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.errors import ConfigError


def _point(
    point_id: str,
    data_type: str,
    register_type: str,
    address: int,
    **extra: object,
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        address=PointAddress(register_type=register_type, address=address, **extra),
        data_type=data_type,
    )


# ---------------------------------------------------------------------------
# parse_point
# ---------------------------------------------------------------------------


class TestParsePoint:
    def test_coil_bool(self) -> None:
        mp = parse_point(_point("sw.on", "bool", "coil", 5))
        assert mp == ModbusPoint(
            point_id="sw.on",
            register_type="coil",
            address=5,
            count=1,
            data_type="bool",
            byte_order="big_endian",
        )

    def test_holding_float32_two_registers(self) -> None:
        mp = parse_point(_point("gen.power", "float32", "holding", 100))
        assert mp.register_type == "holding"
        assert mp.count == 2

    def test_holding_float64_four_registers(self) -> None:
        mp = parse_point(_point("v", "float64", "holding", 100))
        assert mp.count == 4

    def test_holding_uint16_one_register(self) -> None:
        mp = parse_point(_point("v", "uint16", "holding", 100))
        assert mp.count == 1

    def test_input_int32_two_registers(self) -> None:
        mp = parse_point(_point("v", "int32", "input", 200))
        assert mp.register_type == "input"
        assert mp.count == 2

    def test_explicit_count_overrides(self) -> None:
        mp = parse_point(_point("v", "float32", "holding", 100, count=4))
        assert mp.count == 4

    def test_little_endian(self) -> None:
        mp = parse_point(_point("v", "float32", "holding", 100, byte_order="little_endian"))
        assert mp.byte_order == "little_endian"

    def test_default_byte_order_parameter(self) -> None:
        mp = parse_point(
            _point("v", "float32", "holding", 100),
            default_byte_order="little_endian",
        )
        assert mp.byte_order == "little_endian"

    def test_holding_register_alias(self) -> None:
        mp = parse_point(_point("v", "float32", "holding_register", 100))
        assert mp.register_type == "holding"

    def test_input_register_alias(self) -> None:
        mp = parse_point(_point("v", "uint16", "input_register", 100))
        assert mp.register_type == "input"

    def test_missing_register_type_raises(self) -> None:
        point = PointConfig(
            point_id="p",
            address=PointAddress(address=100),
            data_type="float32",
        )
        with pytest.raises(ConfigError, match="register_type"):
            parse_point(point)

    def test_missing_address_raises(self) -> None:
        point = PointConfig(
            point_id="p",
            address=PointAddress(register_type="holding"),
            data_type="float32",
        )
        with pytest.raises(ConfigError, match="address"):
            parse_point(point)

    def test_invalid_register_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="register_type"):
            parse_point(_point("p", "float32", "fifo", 100))

    def test_unsupported_data_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="data_type"):
            parse_point(_point("p", "str", "holding", 100))

    def test_invalid_byte_order_raises(self) -> None:
        with pytest.raises(ConfigError, match="byte_order"):
            parse_point(_point("p", "float32", "holding", 100, byte_order="middle"))


# ---------------------------------------------------------------------------
# group_consecutive_reads
# ---------------------------------------------------------------------------


def _pts(*args: tuple[str, int]) -> list[ModbusPoint]:
    return [
        ModbusPoint(
            point_id=name,
            register_type="holding",
            address=addr,
            count=2,
            data_type="float32",
        )
        for name, addr in args
    ]


class TestGroupConsecutiveReads:
    def test_empty(self) -> None:
        assert group_consecutive_reads([]) == []

    def test_adjacent_points_merge(self) -> None:
        points = _pts(("a", 100), ("b", 102), ("c", 104))
        groups = group_consecutive_reads(points)
        assert len(groups) == 1
        assert [p.point_id for p in groups[0]] == ["a", "b", "c"]

    def test_gap_within_max_gap_merges(self) -> None:
        points = _pts(("a", 100), ("b", 108))  # a occupies 100-101, gap to 108 = 6
        groups = group_consecutive_reads(points, max_gap=8)
        assert len(groups) == 1

    def test_gap_beyond_max_gap_splits(self) -> None:
        points = _pts(("a", 100), ("b", 120))  # gap = 18 > 8
        groups = group_consecutive_reads(points, max_gap=8)
        assert len(groups) == 2
        assert [p.point_id for p in groups[0]] == ["a"]
        assert [p.point_id for p in groups[1]] == ["b"]

    def test_different_register_types_split(self) -> None:
        points = [
            ModbusPoint(
                point_id="a", register_type="holding", address=100, count=1, data_type="uint16"
            ),
            ModbusPoint(
                point_id="b", register_type="input", address=100, count=1, data_type="uint16"
            ),
        ]
        groups = group_consecutive_reads(points)
        assert len(groups) == 2
        assert {g[0].register_type for g in groups} == {"holding", "input"}

    def test_sorts_by_address_within_type(self) -> None:
        points = _pts(("c", 200), ("a", 100), ("b", 102))
        groups = group_consecutive_reads(points)
        assert len(groups) == 2  # (100,102) merged, 200 separate
        assert [p.point_id for p in groups[0]] == ["a", "b"]


def _contiguous(n: int, start: int = 0) -> list[ModbusPoint]:
    """*n* contiguous 1-register holding points from *start*."""
    return [
        ModbusPoint(
            point_id=f"p{start + i}",
            register_type="holding",
            address=start + i,
            count=1,
            data_type="uint16",
        )
        for i in range(n)
    ]


def _span(group: list[ModbusPoint]) -> int:
    """Wire-level request size: ``max(address + count) - min(address)``."""
    return max(p.address + p.count for p in group) - min(p.address for p in group)


class TestGroupConsecutiveReadsMaxRegisters:
    """step23：组合读取必须遵守 Modbus 单请求 125 寄存器上限。"""

    def test_1000_contiguous_points_split_by_125(self) -> None:
        groups = group_consecutive_reads(_contiguous(1000))
        assert len(groups) == 8  # 1000 = 8 × 125
        assert sum(len(g) for g in groups) == 1000
        for g in groups:
            assert _span(g) <= 125

    def test_100_contiguous_points_single_group(self) -> None:
        groups = group_consecutive_reads(_contiguous(100))
        assert len(groups) == 1
        assert _span(groups[0]) == 100

    def test_200_contiguous_points_split_into_two(self) -> None:
        groups = group_consecutive_reads(_contiguous(200))
        assert len(groups) == 2
        assert [len(g) for g in groups] == [125, 75]
        assert _span(groups[0]) == 125
        assert _span(groups[1]) == 75

    def test_split_lands_on_point_boundary_near_limit(self) -> None:
        """124 连续点 + 1 个 gap 内点（跨度 131 > 125）：在点边界切分。

        gap=6 <= max_gap 本来会合并，但合并后跨度超限，必须在最后一个
        装得下的点处切开——切分点永远落在点边界，不切进寄存器中间。
        """
        points = _contiguous(124) + _contiguous(1, start=130)
        groups = group_consecutive_reads(points, max_gap=8)
        assert len(groups) == 2
        assert [p.address for p in groups[0]] == list(range(124))
        assert [p.address for p in groups[1]] == [130]

    def test_max_registers_per_request_parameterized(self) -> None:
        groups = group_consecutive_reads(_contiguous(100), max_registers_per_request=30)
        assert len(groups) == 4  # 100 = 30+30+30+10
        for g in groups:
            assert _span(g) <= 30
