"""Modbus 写入规划：默认禁止合并，授权时也不跨空洞。"""

from __future__ import annotations

from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.mapping import ModbusPoint


def _point(name: str, address: int, *, count: int = 1, kind: str = "holding") -> ModbusPoint:
    return ModbusPoint(
        point_id=name,
        register_type=kind,
        address=address,
        count=count,
        data_type="uint16" if count == 1 else "float32",
        word_order="big_endian",
    )


def test_no_authorization_means_independent_writes() -> None:
    points = [_point("a", 100), _point("b", 101)]
    groups = ModbusDriver._plan_contiguous_writes(points, authorized_point_ids=frozenset())
    assert [[p.point_id for p in group] for group in groups] == [["a"], ["b"]]


def test_authorized_adjacent_registers_can_be_planned_together() -> None:
    points = [_point("a", 100, count=2), _point("b", 102, count=2)]
    groups = ModbusDriver._plan_contiguous_writes(
        points, authorized_point_ids=frozenset({"a", "b"})
    )
    assert [[p.point_id for p in group] for group in groups] == [["a", "b"]]


def test_gap_overlap_and_other_register_type_never_merge() -> None:
    points = [
        _point("a", 100),
        _point("gap", 103),
        _point("overlap", 103),
        _point("coil", 104, kind="coil"),
    ]
    groups = ModbusDriver._plan_contiguous_writes(
        points, authorized_point_ids=frozenset(p.point_id for p in points)
    )
    assert all(len(group) == 1 for group in groups)


def test_write_groups_never_reorder_input() -> None:
    points = [_point("b", 101), _point("a", 100), _point("c", 102)]
    groups = ModbusDriver._plan_contiguous_writes(
        points, authorized_point_ids=frozenset({"a", "b", "c"})
    )
    assert [p.point_id for group in groups for p in group] == ["b", "a", "c"]
