"""Modbus 写入规划：默认禁止合并，授权时也不跨空洞。"""

from __future__ import annotations

import asyncio

import pytest

from core.application import ConfigError
from core.application.protocol_contract import ProtocolWrite
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.modbus.config import parse_modbus_config

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


def test_write_groups_validation_rejects_duplicate_membership() -> None:
    with pytest.raises(ConfigError, match="duplicate"):
        parse_modbus_config(
            ConnectionEndpoint("192.0.2.10", 502),
            {"write_groups": [["a", "b"], ["b", "c"]]},
        )


class _Response:
    def isError(self) -> bool:
        return False


class _Client:
    def __init__(self) -> None:
        self.single: list[tuple[int, int]] = []
        self.multiple: list[tuple[int, list[int]]] = []

    async def write_register(
        self, address: int, value: int, *, device_id: int
    ) -> _Response:
        assert device_id == 1
        self.single.append((address, value))
        return _Response()

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> _Response:
        assert device_id == 1
        self.multiple.append((address, values))
        return _Response()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_contiguous_write_execution_respects_authorization(authorized: bool) -> None:
    driver = object.__new__(ModbusDriver)
    driver._config = parse_modbus_config(
        ConnectionEndpoint("192.0.2.10", 502),
        {"write_groups": [["a", "b"]]} if authorized else {},
    )
    driver._point_table_id = "test"
    driver._points = {"a": _point("a", 100), "b": _point("b", 101)}
    driver._client = _Client()
    driver._connected = True
    driver._lock = asyncio.Lock()

    results = await driver.write_many((ProtocolWrite("a", 10), ProtocolWrite("b", 20)))
    assert all(item.success for item in results)
    if authorized:
        assert driver._client.multiple == [(100, [10, 20])]
        assert driver._client.single == []
    else:
        assert driver._client.multiple == []
        assert driver._client.single == [(100, 10), (101, 20)]


@pytest.mark.asyncio
async def test_contiguous_write_never_fills_register_gap() -> None:
    driver = object.__new__(ModbusDriver)
    driver._config = parse_modbus_config(
        ConnectionEndpoint("192.0.2.10", 502),
        {"write_groups": [["a", "b"]]},
    )
    driver._point_table_id = "test"
    driver._points = {"a": _point("a", 100), "b": _point("b", 103)}
    driver._client = _Client()
    driver._connected = True
    driver._lock = asyncio.Lock()

    await driver.write_many((ProtocolWrite("a", 10), ProtocolWrite("b", 20)))
    assert driver._client.multiple == []
    assert driver._client.single == [(100, 10), (103, 20)]
