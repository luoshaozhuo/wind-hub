"""新 Commander DeviceSession 读写换算与访问控制单元测试。"""

from __future__ import annotations

import pytest

from commander.application.errors import CommandError
from commander.application.session import (
    DeviceSession,
    engineering_value,
    raw_write_value,
)
from core.domain import PointAccess
from tests.support.new_commander import FakeProtocol, make_commander_config


def _session(**kwargs) -> tuple[DeviceSession, FakeProtocol]:
    config = make_commander_config(**kwargs)
    device = next(iter(config.devices.values()))
    table = config.point_table_for_device(device.device_id)
    protocol = FakeProtocol()
    session = DeviceSession(
        device,
        table,
        config.business_points,
        config.point_meta[table.point_table_id],
        protocol,
    )
    return session, protocol


async def test_read_applies_scale_offset():
    session, protocol = _session(scale=0.1, offset=2.0)
    protocol.read_values["p1"] = 30.0
    readings = await session.read_points(["p1"])
    assert readings[0].value == pytest.approx(30.0 * 0.1 + 2.0)
    assert readings[0].source == "modbus"


async def test_read_unknown_point_rejected():
    session, _ = _session()
    with pytest.raises(CommandError, match="unknown point"):
        await session.read_points(["ghost"])


async def test_write_inverse_transform():
    session, protocol = _session(scale=0.1, offset=2.0)
    await session.write_point("p1", 5.0)
    assert len(protocol.writes) == 1
    assert protocol.writes[0].value == pytest.approx((5.0 - 2.0) / 0.1)


async def test_write_identity_passthrough_for_bool():
    session, protocol = _session()
    await session.write_point("p1", True)
    assert protocol.writes[0].value is True


async def test_write_read_only_point_rejected():
    session, _ = _session(access=PointAccess.READ)
    with pytest.raises(CommandError, match="read-only"):
        await session.write_point("p1", 1.0)


async def test_write_protocol_rejection_raises():
    session, protocol = _session()
    protocol.write_success = False
    with pytest.raises(CommandError, match="rejected"):
        await session.write_point("p1", 1.0)


def test_engineering_value_edge_cases():
    session, _ = _session(scale=0.5, offset=1.0)
    point = session.point("p1")
    assert engineering_value(point, None) is None
    assert engineering_value(point, True) is True
    assert engineering_value(point, "text") == "text"
    assert engineering_value(point, 10.0) == pytest.approx(6.0)


def test_raw_write_value_edge_cases():
    session, _ = _session(scale=0.5, offset=1.0)
    point = session.point("p1")
    assert raw_write_value(point, "text") == "text"
    assert raw_write_value(point, True) is True
    assert raw_write_value(point, 6.0) == pytest.approx(10.0)


def test_point_group_lookup():
    session, _ = _session()
    assert [p.point_id for p in session.point_group_points("g")] == ["p1"]
    assert session.point_group_points("unknown") == []
    meta = session.point_meta("p1")
    assert meta.variable_name == "P1"
