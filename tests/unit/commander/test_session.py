"""新 Commander DeviceSession 读写换算与访问控制单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from commander.application.errors import CommandError
from commander.application.session import DeviceSession
from core.application import ProtocolError, ProtocolSample, Quality
from core.domain import PointAccess, engineering_value, raw_write_value
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


async def test_reading_timestamp_required_and_source_passed_through():
    """PointReading 时间戳必填，timestamp_source 随样本透传。"""
    session, protocol = _session()

    device_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    async def read_one(point_id):  # noqa: ANN001, ANN202
        return ProtocolSample(
            point_id=point_id,
            value=1.0,
            quality=Quality.GOOD,
            timestamp=device_time,
            timestamp_source="device",
        )

    protocol.read_one = read_one  # type: ignore[method-assign]
    reading = await session.read_point("p1")

    assert reading.timestamp == device_time
    assert reading.timestamp_source == "device"


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


async def test_read_point_uses_read_one_not_read_many():
    """单点读取必须走 read_one 独立路径，不调用 read_many。"""
    session, protocol = _session(scale=0.1, offset=2.0)
    protocol.read_values["p1"] = 30.0

    async def fail_read_many(point_ids):  # noqa: ANN001, ANN202
        raise AssertionError(f"read_point must not delegate to read_many: {point_ids}")

    protocol.read_many = fail_read_many  # type: ignore[method-assign]
    reading = await session.read_point("p1")
    assert reading.value == pytest.approx(30.0 * 0.1 + 2.0)


async def test_read_points_uses_read_many_not_read_one():
    """批量读取必须走 read_many，不逐点调用 read_one。"""
    session, protocol = _session()

    async def fail_read_one(point_id):  # noqa: ANN001, ANN202
        raise AssertionError(f"read_points must not delegate to read_one: {point_id}")

    protocol.read_one = fail_read_one  # type: ignore[method-assign]
    readings = await session.read_points(["p1", "p1"])
    assert len(readings) == 2


async def test_read_points_rejects_missing_samples():
    """协议少返回样本属于契约违约，必须显式失败而不是静默漏点。"""
    session, protocol = _session()

    async def short_read(point_ids):  # noqa: ANN001, ANN202
        return ()

    protocol.read_many = short_read  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="returned 0 sample"):
        await session.read_points(["p1"])


async def test_read_points_rejects_mismatched_order():
    """协议返回错位的 point_id 属于契约违约。"""
    session, protocol = _session()

    async def shuffled_read(point_ids):  # noqa: ANN001, ANN202
        return tuple(
            ProtocolSample(
                point_id="other",
                value=1.0,
                quality=Quality.GOOD,
                timestamp=datetime.now(UTC),
            )
            for _ in point_ids
        )

    protocol.read_many = shuffled_read  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="mismatch"):
        await session.read_points(["p1"])


async def test_read_points_keeps_duplicate_points_positional():
    """重复点按出现位置逐位返回。"""
    session, protocol = _session()
    protocol.read_values["p1"] = 4.0
    readings = await session.read_points(["p1", "p1"])
    assert [r.point_id for r in readings] == ["p1", "p1"]
    assert all(r.value == pytest.approx(4.0) for r in readings)


def test_point_group_lookup():
    session, _ = _session()
    assert [p.point_id for p in session.point_group_points("g")] == ["p1"]
    assert session.point_group_points("unknown") == []
    meta = session.point_meta("p1")
    assert meta.variable_name == "P1"
