"""新 Collector CollectorDeviceSession 与 PollingAcquisitionHandle 单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.session import AcquisitionMode, PollingAcquisitionHandle
from core.application import ConfigError, ProtocolCapability, Quality
from tests.support.new_collector import (
    CollectorFakeProtocol,
    make_collector_config,
    make_session,
)


def _caps(*caps: ProtocolCapability) -> frozenset[ProtocolCapability]:
    return frozenset(caps)


# ---------------------------------------------------------------------------
# 选点与读取
# ---------------------------------------------------------------------------


async def test_point_ids_filtered_by_group():
    session = make_session(make_collector_config(), CollectorFakeProtocol())
    assert session.point_ids("g") == ["p1"]
    assert session.point_ids("other") == []


async def test_read_applies_engineering_transform_and_stamps_device():
    proto = CollectorFakeProtocol()
    proto.read_values["p1"] = 10.0
    session = make_session(make_collector_config(scale=2.0, offset=1.0), proto)
    values = await session.read("g")
    assert len(values) == 1
    assert values[0].value == 21.0
    assert values[0].device_id == "dev1"
    assert values[0].source == "modbus"
    assert values[0].quality is Quality.GOOD


async def test_read_identity_transform_keeps_raw():
    proto = CollectorFakeProtocol()
    proto.read_values["p1"] = 7.5
    session = make_session(make_collector_config(), proto)
    values = await session.read("g")
    assert values[0].value == 7.5


# ---------------------------------------------------------------------------
# 采集模式推导
# ---------------------------------------------------------------------------


def test_modbus_session_polls():
    session = make_session(make_collector_config(protocol="modbus"), CollectorFakeProtocol())
    assert session.acquisition_mode is AcquisitionMode.POLL


def test_iec104_session_subscribes():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.READ, ProtocolCapability.SUBSCRIBE)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    assert session.acquisition_mode is AcquisitionMode.SUBSCRIBE


def test_ads_polls_unless_subscribe_enabled():
    caps = _caps(ProtocolCapability.READ, ProtocolCapability.SUBSCRIBE)
    proto = CollectorFakeProtocol()
    proto.capabilities_value = caps
    poll_session = make_session(make_collector_config(protocol="ads"), proto)
    assert poll_session.acquisition_mode is AcquisitionMode.POLL

    sub_session = make_session(
        make_collector_config(protocol="ads", ads_subscribe=True), CollectorFakeProtocol()
    )
    sub_session._protocol.capabilities_value = caps  # type: ignore[attr-defined]
    assert sub_session.acquisition_mode is AcquisitionMode.SUBSCRIBE


# ---------------------------------------------------------------------------
# start_acquisition——POLL
# ---------------------------------------------------------------------------


async def test_poll_requires_positive_interval():
    session = make_session(make_collector_config(), CollectorFakeProtocol())
    with pytest.raises(ConfigError, match="interval"):
        await session.start_acquisition(point_group="g", interval=None, acquire=lambda: None)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="interval"):
        await session.start_acquisition(point_group="g", interval=0.0, acquire=lambda: None)  # type: ignore[arg-type]


async def test_poll_requires_acquire_callback():
    session = make_session(make_collector_config(), CollectorFakeProtocol())
    with pytest.raises(ConfigError, match="acquire"):
        await session.start_acquisition(point_group="g", interval=1.0)


async def test_sequential_device_rejects_scheduled_collection():
    config = make_collector_config(protocol="ads")
    proto = CollectorFakeProtocol()
    session = make_session(config, proto)
    session._supports_scheduled_collection = False  # type: ignore[attr-defined]
    with pytest.raises(ConfigError, match="does not support scheduled collection"):
        await session.start_acquisition(point_group="g", interval=1.0, acquire=lambda: None)  # type: ignore[arg-type]


async def test_poll_handle_runs_acquire_and_closes():
    session = make_session(make_collector_config(), CollectorFakeProtocol())
    ticks: list[float] = []

    async def acquire() -> None:
        ticks.append(asyncio.get_running_loop().time())

    handle = await session.start_acquisition(point_group="g", interval=0.02, acquire=acquire)
    await asyncio.sleep(0.09)
    await handle.close()
    count_at_close = len(ticks)
    assert count_at_close >= 2
    await asyncio.sleep(0.05)
    assert len(ticks) == count_at_close  # close 后不再触发
    await handle.close()  # 幂等


# ---------------------------------------------------------------------------
# start_acquisition——SUBSCRIBE
# ---------------------------------------------------------------------------


async def test_subscribe_requires_on_data():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.SUBSCRIBE)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    with pytest.raises(ConfigError, match="on_data"):
        await session.start_acquisition(point_group="g", interval=None)


async def test_subscribe_registers_points_and_interrogates_when_capable():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.SUBSCRIBE, ProtocolCapability.INTERROGATE)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    received: list[list] = []

    async def on_data(batch):
        received.append(batch)

    handle = await session.start_acquisition(point_group="g", interval=None, on_data=on_data)
    assert proto.subscriptions[0][0] == ("p1",)
    assert proto.interrogate_calls == 1  # 订阅建立后自动总召

    await proto.emit(0, "p1", 5.0)
    assert len(received) == 1
    assert received[0][0].device_id == "dev1"  # 会话统一补盖设备身份
    assert received[0][0].value == 5.0
    await handle.close()


async def test_subscribe_skips_interrogate_when_not_capable():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.SUBSCRIBE)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    handle = await session.start_acquisition(
        point_group="g",
        interval=None,
        on_data=lambda b: None,  # type: ignore[arg-type]
    )
    assert proto.interrogate_calls == 0
    await handle.close()


# ---------------------------------------------------------------------------
# PollingAcquisitionHandle——fixed-rate 语义
# ---------------------------------------------------------------------------


async def test_fixed_rate_no_drift_and_overrun_skip():
    stats: list[tuple[float, bool, int]] = []
    calls = 0

    async def acquire() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.035)  # 超过 interval 的一半但不到 2 倍

    handle = PollingAcquisitionHandle(0.02, acquire, on_stats=lambda *a: stats.append(a))
    await handle.start()
    await asyncio.sleep(0.15)
    await handle.close()
    assert calls >= 3
    assert any(overrun for _, overrun, _ in stats)


async def test_acquire_exception_does_not_stop_polling():
    calls = 0

    async def acquire() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("boom")

    handle = PollingAcquisitionHandle(0.02, acquire)
    await handle.start()
    await asyncio.sleep(0.09)
    await handle.close()
    assert calls >= 3


async def test_close_idempotent_and_start_idempotent():
    async def acquire() -> None:
        await asyncio.sleep(0)

    handle = PollingAcquisitionHandle(0.05, acquire)
    await handle.start()
    await handle.start()  # 不产生第二个协程
    task = handle._task
    await handle.close()
    await handle.close()
    assert handle._task is None and task is not None and task.done()


async def test_raw_driver_path_skips_protocol_sample_wrapping():
    proto = CollectorFakeProtocol()
    proto.read_values["p1"] = 7.0
    async def read_raw(point_ids):
        assert point_ids == ["p1"]
        return ((7.0, Quality.GOOD),)

    async def reject_legacy_read(_point_ids):
        raise AssertionError("raw-capable driver should bypass ProtocolSample read")

    proto.read_raw = read_raw
    proto.read = reject_legacy_read
    session = make_session(make_collector_config(scale=2.0, offset=1.0), proto)
    values = await session.read("g")
    assert len(values) == 1
    assert values[0].value == 15.0
    assert values[0].device_id == "dev1"
    assert values[0].quality is Quality.GOOD


async def test_point_group_cache_invalidated_by_set_points():
    """相同点组不重复筛选；配置刷新后必须使用新点组。"""
    first = make_collector_config(point_groups=("g",))
    second = make_collector_config(point_groups=("changed",))
    session = make_session(first, CollectorFakeProtocol())
    assert session.point_ids("g") == ["p1"]
    assert session.point_ids("g") == ["p1"]
    assert session._point_group_cache["g"] == ("p1",)
    view = second.device_view("dev1")
    session.set_points(view.point_table, view.point_meta)
    assert session.point_ids("g") == []
    assert session.point_ids("changed") == ["p1"]


async def test_raw_value_conversion_preserves_bad_and_bool():
    proto = CollectorFakeProtocol()
    session = make_session(make_collector_config(scale=3.0, offset=1.0), proto)
    bad = session.to_point_values_raw(["p1"], ((None, Quality.BAD),))
    assert bad[0].value is None
    assert bad[0].quality is Quality.BAD
    flag = session.to_point_values_raw(["p1"], ((True, Quality.GOOD),))
    assert flag[0].value is True
    with pytest.raises(ValueError, match="length"):
        session.to_point_values_raw(["p1"], ())
