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


@pytest.mark.asyncio
async def test_modbus_read_plan_cache_reuse_eviction_and_reload() -> None:
    """同样测点 ID 序列复用分组，缓存有界；点表更新后仅淘汰受影响计划。"""
    from core.application.protocol_contract import PointTableUpdateStatus
    from core.domain import ConnectionEndpoint, PointTable, Protocol
    from core.domain.identities import PointTableId
    from core.infrastructure.protocol.modbus.driver import ModbusDriver

    points = {f"p{i}": _point(f"p{i}", address=i) for i in range(40)}
    table = PointTable(PointTableId("cache"), Protocol("modbus"), points)
    driver = ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), table, {})

    class ReadSession:
        def is_open(self) -> bool:
            return True

        async def read(
            self, register_type: str, address: int, count: int, slave_id: int
        ) -> list[int]:
            assert register_type == "holding"
            assert slave_id == 1
            return [address + i for i in range(count)]

    session = ReadSession()
    driver._session = session  # type: ignore[assignment]
    assert [v.value for v in await driver.read_many(["p1", "p2"])] == [1, 2]
    key = ("p1", "p2")
    cached = driver._read_plan_cache[key]
    await driver.read_many(list(key))
    assert driver._read_plan_cache[key] is cached

    for i in range(40):
        await driver.read_many([f"p{i}"])
    assert len(driver._read_plan_cache) <= 32

    # 重新载入未受影响计划与一个将被变更点的计划，验证差异化淘汰。
    await driver.read_many(list(key))
    assert driver._read_plan_cache[key] == cached
    await driver.read_many(["p39"])
    assert ("p39",) in driver._read_plan_cache

    # 新点表：p39 改地址并新增 p40；其余点不变。
    new_points = dict(points)
    new_points["p39"] = _point("p39", address=1000)
    new_points["p40"] = _point("p40", address=40)
    new_table = PointTable(PointTableId("cache-2"), Protocol("modbus"), new_points)

    result = await driver.update_point_table(new_table, timeout=5.0)
    assert result.status is PointTableUpdateStatus.SUCCESS
    assert result.applied is True
    assert result.active_version == 2
    # 未受影响的计划保留复用；受 p39/p40 影响的计划被淘汰。
    assert driver._read_plan_cache.get(key) == cached
    assert ("p39",) not in driver._read_plan_cache
    assert all("p40" not in plan_key for plan_key in driver._read_plan_cache)


@pytest.mark.asyncio
async def test_gap_rejection_falls_back_and_caches_safe_read_plan() -> None:
    """严格 Modbus Server 拒绝跨空洞读取后，连续地址计划可被复用。"""
    from core.domain import ConnectionEndpoint, PointTable, Protocol
    from core.domain.identities import PointTableId
    from core.infrastructure.protocol.modbus.driver import ModbusDriver
    from core.infrastructure.protocol.modbus.session import _ModbusIllegalDataAddressError

    points = {
        "first": _point("first", address=0),
        "second": _point("second", address=6),
    }
    table = PointTable(PointTableId("sparse"), Protocol("modbus"), points)
    driver = ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), table, {})

    class StrictSession:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def is_open(self) -> bool:
            return True

        async def read(
            self, register_type: str, address: int, count: int, slave_id: int
        ) -> list[int]:
            assert register_type == "holding" and slave_id == 1
            self.calls.append((address, count))
            if count > 1:
                raise _ModbusIllegalDataAddressError(
                    "Modbus exception response: IllegalDataAddress"
                )
            return [address + 100]

    session = StrictSession()
    driver._session = session  # type: ignore[assignment]
    first = await driver.read_many(["first", "second"])
    assert [sample.value for sample in first] == [100, 106]
    assert session.calls == [(0, 7), (0, 1), (6, 1)]
    assert [
        [p.point_id for p in group] for group in driver._read_plan_cache[("first", "second")]
    ] == [["first"], ["second"]]
    second = await driver.read_many(["first", "second"])
    assert [sample.value for sample in second] == [100, 106]
    assert session.calls[-2:] == [(0, 1), (6, 1)]
    assert session.calls.count((0, 7)) == 1


@pytest.mark.asyncio
async def test_gap_read_does_not_retry_connection_failure() -> None:
    """链路级错误不应被误识别为服务器拒绝地址并产生额外读请求。"""
    from core.application.errors import ProtocolConnectionError
    from core.domain import ConnectionEndpoint, PointTable, Protocol
    from core.domain.identities import PointTableId
    from core.infrastructure.protocol.modbus.driver import ModbusDriver

    table = PointTable(
        PointTableId("sparse-failure"),
        Protocol("modbus"),
        {
            "first": _point("first", address=0),
            "second": _point("second", address=6),
        },
    )
    driver = ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), table, {})

    class BrokenSession:
        def __init__(self) -> None:
            self.calls = 0

        def is_open(self) -> bool:
            return True

        async def read(
            self, register_type: str, address: int, count: int, slave_id: int
        ) -> list[int]:
            self.calls += 1
            raise ProtocolConnectionError("connection lost")

    session = BrokenSession()
    driver._session = session  # type: ignore[assignment]
    with pytest.raises(ProtocolConnectionError, match="connection lost"):
        await driver.read_many(["first", "second"])
    assert session.calls == 1


@pytest.mark.asyncio
async def test_sparse_point_table_online_verification_uses_safe_fallback() -> None:
    """在线升级点表的预检也必须兼容严格的 Modbus 地址范围。"""
    from core.application.protocol_contract import PointTableUpdateStatus
    from core.domain import ConnectionEndpoint, PointTable, Protocol
    from core.domain.identities import PointTableId
    from core.infrastructure.protocol.modbus.driver import ModbusDriver
    from core.infrastructure.protocol.modbus.session import _ModbusIllegalDataAddressError

    original = PointTable(
        PointTableId("before"),
        Protocol("modbus"),
        {"a": _point("a", address=0), "b": _point("b", address=1)},
    )
    updated = PointTable(
        PointTableId("after"),
        Protocol("modbus"),
        {"a": _point("a", address=3), "b": _point("b", address=9)},
    )
    driver = ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), original, {})

    class StrictSession:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def is_open(self) -> bool:
            return True

        async def read(
            self, register_type: str, address: int, count: int, slave_id: int
        ) -> list[int]:
            self.calls.append((address, count))
            if count > 1:
                raise _ModbusIllegalDataAddressError("address hole")
            return [123]

    session = StrictSession()
    driver._session = session  # type: ignore[assignment]
    result = await driver.update_point_table(updated, timeout=5.0)
    assert result.status is PointTableUpdateStatus.SUCCESS
    assert result.applied is True
    assert session.calls == [(3, 7), (3, 1), (9, 1)]


@pytest.mark.asyncio
async def test_unrelated_modbus_protocol_error_does_not_retry_gap() -> None:
    """仅 IllegalDataAddress 可触发拆分，其他异常必须保持可见。"""
    from core.application.errors import ProtocolError
    from core.domain import ConnectionEndpoint, PointTable, Protocol
    from core.domain.identities import PointTableId
    from core.infrastructure.protocol.modbus.driver import ModbusDriver

    table = PointTable(
        PointTableId("unsupported-function"),
        Protocol("modbus"),
        {"a": _point("a", address=0), "b": _point("b", address=6)},
    )
    driver = ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), table, {})

    class UnsupportedSession:
        def __init__(self) -> None:
            self.calls = 0

        def is_open(self) -> bool:
            return True

        async def read(
            self, register_type: str, address: int, count: int, slave_id: int
        ) -> list[int]:
            self.calls += 1
            raise ProtocolError("illegal function")

    session = UnsupportedSession()
    driver._session = session  # type: ignore[assignment]
    with pytest.raises(ProtocolError, match="illegal function"):
        await driver.read_many(["a", "b"])
    assert session.calls == 1
