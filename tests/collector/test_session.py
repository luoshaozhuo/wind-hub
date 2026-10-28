"""新 Collector CollectorDeviceSession 与 PollingAcquisitionHandle 单元测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from collector.application.session import AcquisitionMode, PollingAcquisitionHandle
from core.application import (
    ConfigError,
    InterrogationRequest,
    ProtocolCapability,
    Quality,
)
from core.domain.identities import DeviceId
from tests.support.new_collector import (
    CollectorFakeProtocol,
    make_collector_config,
    make_session,
)

pytestmark = pytest.mark.unit


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


def test_iec104_session_listens():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(
        ProtocolCapability.READ,
        ProtocolCapability.LISTEN,
        ProtocolCapability.INTERROGATE,
    )
    session = make_session(make_collector_config(protocol="iec104"), proto)
    assert session.acquisition_mode is AcquisitionMode.LISTEN


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
    async def noop() -> None:
        return None

    session = make_session(make_collector_config(), CollectorFakeProtocol())
    with pytest.raises(ConfigError, match="interval"):
        await session.start_acquisition(point_group="g", interval=None, acquire=noop)
    with pytest.raises(ConfigError, match="interval"):
        await session.start_acquisition(point_group="g", interval=0.0, acquire=noop)


async def test_poll_requires_acquire_callback():
    session = make_session(make_collector_config(), CollectorFakeProtocol())
    with pytest.raises(ConfigError, match="acquire"):
        await session.start_acquisition(point_group="g", interval=1.0)


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
# start_acquisition——SUBSCRIBE（ADS 原生订阅）
# ---------------------------------------------------------------------------


async def test_subscribe_requires_on_data():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.SUBSCRIBE)
    session = make_session(make_collector_config(protocol="ads", ads_subscribe=True), proto)
    with pytest.raises(ConfigError, match="on_data"):
        await session.start_acquisition(point_group="g", interval=None)


async def test_subscribe_registers_native_subscription_without_interrogate():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.SUBSCRIBE)
    session = make_session(make_collector_config(protocol="ads", ads_subscribe=True), proto)
    received: list[tuple[Any, ...]] = []

    async def on_data(batch):
        received.append(batch)

    handle = await session.start_acquisition(point_group="g", interval=1.0, on_data=on_data)
    # ADS 原生订阅：注册 notification，cycle_time 透传，不触发总召。
    assert proto.subscriptions[0][0] == ("p1",)
    assert proto.subscriptions[0][2] == 1.0
    assert proto.interrogate_calls == []

    await proto.emit(0, "p1", 5.0)
    assert len(received) == 1
    assert received[0][0].device_id == "dev1"  # 会话统一补盖设备身份
    assert received[0][0].value == 5.0
    await handle.close()


# ---------------------------------------------------------------------------
# start_acquisition——LISTEN（IEC104 本地监听 + 显式总召）
# ---------------------------------------------------------------------------


async def test_listen_requires_on_data():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.LISTEN)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    with pytest.raises(ConfigError, match="on_data"):
        await session.start_acquisition(point_group="g", interval=None)


async def test_listen_registers_points_and_interrogates_after_listen():
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.LISTEN, ProtocolCapability.INTERROGATE)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    received: list[tuple[Any, ...]] = []

    async def on_data(batch):
        received.append(batch)

    handle = await session.start_acquisition(point_group="g", interval=None, on_data=on_data)
    assert proto.listeners[0][0] == ("p1",)
    assert proto.subscriptions == []  # 严禁把 listen 伪装成 subscribe
    # 监听建立后 Collector 显式全 CA 总召（默认 station 请求，不限定 CA/组）
    assert proto.interrogate_calls == [InterrogationRequest()]

    await proto.emit_listener(0, "p1", 5.0)
    assert len(received) == 1
    assert received[0][0].device_id == "dev1"
    assert received[0][0].value == 5.0
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


async def test_read_uses_read_many_batch_interface():
    proto = CollectorFakeProtocol()
    proto.read_values["p1"] = 7.0
    calls: list[tuple[str, ...]] = []

    base_read_many = proto.read_many

    async def recording_read_many(point_ids):
        calls.append(tuple(point_ids))
        return await base_read_many(point_ids)

    proto.read_many = recording_read_many  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    session = make_session(make_collector_config(scale=2.0, offset=1.0), proto)
    values = await session.read("g")
    assert calls == [("p1",)]
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
    table = second.point_table_for_device(DeviceId("dev1"))
    session.set_points(table)
    assert session.point_ids("g") == []
    assert session.point_ids("changed") == ["p1"]


async def test_read_preserves_bad_quality_and_bool():
    from core.application import ProtocolSample

    proto = CollectorFakeProtocol()

    async def read_many(point_ids):
        assert list(point_ids) == ["p1"]
        return (
            ProtocolSample(
                point_id="p1",
                value=None,
                quality=Quality.BAD,
                timestamp=datetime.now(UTC),
            ),
        )

    proto.read_many = read_many  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    session = make_session(make_collector_config(scale=3.0, offset=1.0), proto)
    bad = await session.read("g")
    assert bad[0].value is None
    assert bad[0].quality is Quality.BAD

    async def read_bool(point_ids):
        return (
            ProtocolSample(
                point_id="p1",
                value=True,
                quality=Quality.GOOD,
                timestamp=datetime.now(UTC),
            ),
        )

    proto.read_many = read_bool  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    flag = await session.read("g")
    assert flag[0].value is True


async def test_read_passes_through_protocol_timestamp_and_source():
    """协议样本的时间戳与来源（device/local）透传到 PointValue，不再兜底补时间。"""
    from core.application import ProtocolSample

    device_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    proto = CollectorFakeProtocol()

    async def read_many(point_ids):
        return (
            ProtocolSample(
                point_id="p1",
                value=1.0,
                quality=Quality.GOOD,
                timestamp=device_time,
                timestamp_source="device",
            ),
        )

    proto.read_many = read_many  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    session = make_session(make_collector_config(), proto)
    values = await session.read("g")

    assert values[0].timestamp == device_time
    assert values[0].timestamp_source == "device"


async def test_set_points_failure_keeps_session_on_old_table():
    """Driver 先行更新：Driver 校验失败时会话仍持旧表，不留不一致状态。"""
    first = make_collector_config(point_groups=("g",))
    second = make_collector_config(point_groups=("changed",))
    proto = CollectorFakeProtocol()
    session = make_session(first, proto)
    old_table = session.point_table

    def _reject(_point_table):
        raise ConfigError("driver rejected new table")

    proto.update_point_table = _reject  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    table = second.point_table_for_device(DeviceId("dev1"))
    with pytest.raises(ConfigError, match="driver rejected"):
        session.set_points(table)

    assert session.point_table is old_table
    assert session.point_ids("g") == ["p1"]


async def test_set_points_updates_driver_before_session_state():
    """成功路径同样 Driver 先行：会话换表时 Driver 映射已是新表。"""
    first = make_collector_config(point_groups=("g",))
    second = make_collector_config(point_groups=("changed",))
    proto = CollectorFakeProtocol()
    session = make_session(first, proto)
    table = second.point_table_for_device(DeviceId("dev1"))
    session.set_points(table)
    assert proto.point_table_updates[-1] is table
    assert session.point_table is table


async def test_listen_interrogation_failure_closes_registered_handle() -> None:
    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.LISTEN, ProtocolCapability.INTERROGATE)
    session = make_session(make_collector_config(protocol="iec104"), proto)
    closed = 0

    class TrackedHandle:
        async def close(self) -> None:
            nonlocal closed
            closed += 1

    async def listen(_points, _callback, *, interval=None):
        return TrackedHandle()

    async def failed_interrogation(_request=None):
        raise RuntimeError("injected interrogation failure")

    proto.listen = listen  # type: ignore[method-assign]
    proto.interrogate = failed_interrogation  # type: ignore[method-assign]

    async def on_data(_batch) -> None:
        return None

    with pytest.raises(RuntimeError, match="injected interrogation failure"):
        await session.start_acquisition(point_group="g", interval=None, on_data=on_data)
    assert closed == 1


async def test_listen_preserves_protocol_batch_to_collector() -> None:
    """一个 Push 批次只执行一次工程值转换与业务回调。"""
    from core.application.protocol_contract import ProtocolSample

    proto = CollectorFakeProtocol()
    proto.capabilities_value = _caps(ProtocolCapability.LISTEN, ProtocolCapability.INTERROGATE)
    session = make_session(make_collector_config(protocol="iec104", scale=2.0), proto)
    batches: list[list[Any]] = []

    async def on_data(values: list[Any]) -> None:
        batches.append(values)

    handle = await session.start_acquisition(point_group="g", interval=None, on_data=on_data)
    _, callback, _ = proto.listeners[0]
    now = datetime.now(UTC)
    await callback(
        (
            ProtocolSample(point_id="p1", value=1.0, quality=Quality.GOOD, timestamp=now),
            ProtocolSample(point_id="p1", value=2.0, quality=Quality.GOOD, timestamp=now),
        )
    )
    assert len(batches) == 1
    assert [value.value for value in batches[0]] == [2.0, 4.0]
    await handle.close()
