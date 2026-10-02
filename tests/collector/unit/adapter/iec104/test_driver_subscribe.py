"""Unit tests for IEC104 driver — subscribe and dispatch."""

from __future__ import annotations

import asyncio

import pytest

from wind_hub.adapter.outbound.protocol.iec104.codec.asdu import ASDU
from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    InterrogationCommand,
    MeasuredValueShort,
    SinglePoint,
    encode_c_ic_na_1,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
)
from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver
from wind_hub.adapter.outbound.protocol.iec104.subscriptions import (
    SubscriptionRegistry,
)
from wind_hub_core.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import AcquisitionMode

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_device_config(**extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-device",
        protocol="iec104",
        point_table="t1",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=2404,
            extensions=dict(extensions),
        ),
    )


def _make_point_config(
    point_id: str,
    ioa: int,
    data_type: str = "float32",
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(ioa=ioa),
        data_type=data_type,
    )


# ===========================================================================
# SubscriptionRegistry (standalone)
# ===========================================================================


class TestSubscriptionRegistry:
    async def test_global_subscribe_receives_all(self) -> None:
        """A global subscriber receives values for any IOA."""
        reg = SubscriptionRegistry()
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        reg.subscribe([], cb, lambda _: None)

        pv1 = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        pv2 = PointValue(
            device_id="d1",
            point_id="p2",
            value=2.0,
            quality=Quality.GOOD,
            source="iec104",
        )

        await reg.dispatch(pv1, 100)
        await reg.dispatch(pv2, 200)
        # dispatch() uses ensure_future — yield so tasks run.
        await asyncio.sleep(0)

        assert len(received) == 2
        assert received[0].point_id == "p1"
        assert received[1].point_id == "p2"

    async def test_per_ioa_subscribe(self) -> None:
        """Per-IOA subscriber only receives values for matching IOA."""
        reg = SubscriptionRegistry()
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        refs = [PointRef(device_id="d1", point_id="p1")]
        reg.subscribe(refs, cb, lambda _: 100)

        pv1 = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        pv2 = PointValue(
            device_id="d1",
            point_id="p2",
            value=2.0,
            quality=Quality.GOOD,
            source="iec104",
        )

        await reg.dispatch(pv1, 100)  # Matches IOA
        await reg.dispatch(pv2, 200)  # Different IOA
        await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0].point_id == "p1"

    async def test_multiple_subscribers_same_ioa(self) -> None:
        """Multiple subscribers on the same IOA all receive the value."""
        reg = SubscriptionRegistry()
        r1: list[PointValue] = []
        r2: list[PointValue] = []

        async def cb1(pv: PointValue) -> None:
            r1.append(pv)

        async def cb2(pv: PointValue) -> None:
            r2.append(pv)

        refs = [PointRef(device_id="d1", point_id="p1")]
        reg.subscribe(refs, cb1, lambda _: 100)
        reg.subscribe(refs, cb2, lambda _: 100)

        pv = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await reg.dispatch(pv, 100)
        await asyncio.sleep(0)

        assert len(r1) == 1
        assert len(r2) == 1

    async def test_unknown_point_skipped(self) -> None:
        """Points that resolve to None are silently skipped."""
        reg = SubscriptionRegistry()
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        refs = [PointRef(device_id="d1", point_id="unknown")]
        reg.subscribe(refs, cb, lambda _: None)  # resolver returns None

        pv = PointValue(
            device_id="d1",
            point_id="unknown",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await reg.dispatch(pv, 999)
        # No subscriber registered, so nothing dispatched.
        assert len(received) == 0

    async def test_callback_exception_does_not_crash_dispatch(self) -> None:
        """A callback that raises does not prevent other callbacks from running."""
        reg = SubscriptionRegistry()
        good_received: list[PointValue] = []

        async def bad_cb(pv: PointValue) -> None:
            raise RuntimeError("boom")

        async def good_cb(pv: PointValue) -> None:
            good_received.append(pv)

        refs = [PointRef(device_id="d1", point_id="p1")]
        reg.subscribe(refs, bad_cb, lambda _: 100)
        reg.subscribe(refs, good_cb, lambda _: 100)

        pv = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await reg.dispatch(pv, 100)
        # Give the ensure_future tasks time to run.
        await asyncio.sleep(0.05)

        # The good callback should still have fired.
        assert len(good_received) == 1

    async def test_global_and_ioa_both_receive(self) -> None:
        """When both global and IOA subscribers exist, both are called."""
        reg = SubscriptionRegistry()
        global_received: list[PointValue] = []
        ioa_received: list[PointValue] = []

        async def global_cb(pv: PointValue) -> None:
            global_received.append(pv)

        async def ioa_cb(pv: PointValue) -> None:
            ioa_received.append(pv)

        reg.subscribe([], global_cb, lambda _: None)
        refs = [PointRef(device_id="d1", point_id="p1")]
        reg.subscribe(refs, ioa_cb, lambda _: 100)

        pv = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await reg.dispatch(pv, 100)
        await asyncio.sleep(0)

        assert len(global_received) == 1
        assert len(ioa_received) == 1

    async def test_close_unsubscribes_only_own_callback(self) -> None:
        """关闭订阅句柄只注销自己的回调——同 IOA 的其他订阅不受影响。"""
        reg = SubscriptionRegistry()
        r1: list[PointValue] = []
        r2: list[PointValue] = []

        async def cb1(pv: PointValue) -> None:
            r1.append(pv)

        async def cb2(pv: PointValue) -> None:
            r2.append(pv)

        refs = [PointRef(device_id="d1", point_id="p1")]
        sub_a = reg.subscribe(refs, cb1, lambda _: 100)
        reg.subscribe(refs, cb2, lambda _: 100)

        await sub_a.close()
        assert reg.ioa_count == 1

        pv = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await reg.dispatch(pv, 100)
        await asyncio.sleep(0)

        assert r1 == []
        assert len(r2) == 1

    async def test_close_is_idempotent(self) -> None:
        """重复 close 不报错、不重复注销。"""
        reg = SubscriptionRegistry()

        async def cb(pv: PointValue) -> None:
            pass

        sub = reg.subscribe([], cb, lambda _: None)
        await sub.close()
        await sub.close()
        assert reg.global_count == 0

    async def test_no_subscribers_no_error(self) -> None:
        """Dispatching when no one is subscribed should not raise."""
        reg = SubscriptionRegistry()
        pv = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await reg.dispatch(pv, 100)
        # Should not raise.

    def test_stats(self) -> None:
        """global_count, ioa_count, unique_ioa_count are correct."""
        reg = SubscriptionRegistry()
        assert reg.global_count == 0
        assert reg.ioa_count == 0
        assert reg.unique_ioa_count == 0

        async def cb(pv: PointValue) -> None:
            pass

        # Global
        reg.subscribe([], cb, lambda _: None)
        assert reg.global_count == 1

        # Two IOAs
        reg.subscribe(
            [PointRef(device_id="d1", point_id="p1")],
            cb,
            lambda _: 100,
        )
        reg.subscribe(
            [PointRef(device_id="d1", point_id="p2")],
            cb,
            lambda _: 200,
        )
        assert reg.ioa_count == 2
        assert reg.unique_ioa_count == 2

        # Same IOA, second subscriber
        reg.subscribe(
            [PointRef(device_id="d1", point_id="p1")],
            cb,
            lambda _: 100,
        )
        assert reg.ioa_count == 3
        assert reg.unique_ioa_count == 2  # Still 2 unique IOAs


# ===========================================================================
# Driver subscribe (without real connection)
# ===========================================================================


class TestDriverSubscribe:
    async def test_subscribe_delegates_to_registry(self) -> None:
        """subscribe() passes through to SubscriptionRegistry."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        await driver.subscribe([PointRef(device_id="d1", point_id="p1")], cb)
        # After subscribing, dispatch should work.
        await driver._subscriptions.dispatch(
            PointValue(
                device_id="d1",
                point_id="p1",
                value=1.0,
                quality=Quality.GOOD,
                source="iec104",
            ),
            100,
        )
        # Give the task a moment to run.
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].point_id == "p1"

    async def test_acquisition_mode_is_subscribe(self) -> None:
        """IEC104 是订阅式采集——POLL 分支永远不会选中它。"""
        driver = IEC104Driver(_make_device_config())
        assert driver.acquisition_mode is AcquisitionMode.SUBSCRIBE

    async def test_stop_handle_unsubscribes_without_closing_driver(self) -> None:
        """停止一个订阅句柄只注销该订阅——驱动与其他订阅保持活跃。"""
        driver = IEC104Driver(_make_device_config())
        driver.set_points_mapping([_make_point_config("p1", 100)])

        r1: list[PointValue] = []
        r2: list[PointValue] = []

        async def cb1(pv: PointValue) -> None:
            r1.append(pv)

        async def cb2(pv: PointValue) -> None:
            r2.append(pv)

        ref = PointRef(device_id="d1", point_id="p1")
        sub_a = await driver.subscribe([ref], cb1)
        await driver.subscribe([ref], cb2)

        await sub_a.close()

        pv = PointValue(
            device_id="d1",
            point_id="p1",
            value=1.0,
            quality=Quality.GOOD,
            source="iec104",
        )
        await driver._subscriptions.dispatch(pv, 100)
        await asyncio.sleep(0.05)

        assert r1 == []
        assert len(r2) == 1


class _FakeSession:
    """IEC104Session 替身——记录 send_asdu 调用。"""

    def __init__(self, started: bool = True) -> None:
        self.is_started = started
        self.sent: list[ASDU] = []

    def send_asdu(self, asdu: ASDU) -> None:
        self.sent.append(asdu)


class TestDriverInterrogate:
    async def test_interrogate_sends_general_interrogation(self) -> None:
        """interrogate 发送一次 C_IC_NA_1 / ACTIVATION / QOI=20 总召。"""
        driver = IEC104Driver(_make_device_config())
        session = _FakeSession()
        driver._session = session  # noqa: SLF001 —— 绕过真实 TCP 连接

        await driver.interrogate()

        assert len(session.sent) == 1
        asdu = session.sent[0]
        assert asdu.type_id is TypeID.C_IC_NA_1
        assert asdu.cause is CauseOfTransmission.ACTIVATION
        assert len(asdu.objects) == 1
        cmd = asdu.objects[0]
        assert isinstance(cmd, InterrogationCommand)
        assert cmd.ioa == 0
        # QOI=20（station interrogation）由编码器写入——经编码字节流验证
        assert encode_c_ic_na_1(cmd)[-1] == 20

    async def test_interrogate_not_connected_raises(self) -> None:
        """未连接时 interrogate 显式失败（不静默吞掉）。"""
        driver = IEC104Driver(_make_device_config())
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.interrogate()

    async def test_interrogate_session_stopped_raises(self) -> None:
        """session 存在但已停止同样视为未连接。"""
        driver = IEC104Driver(_make_device_config())
        driver._session = _FakeSession(started=False)  # noqa: SLF001
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.interrogate()


# ===========================================================================
# _dispatch_point_values (via _on_asdu_received)
# ===========================================================================


class TestDispatchPointValues:
    """Test the ASDU→PointValue→subscriber dispatch chain."""

    async def test_spontaneous_cot_dispatches(self) -> None:
        """ASDU with COT=SPONTANEOUS triggers subscriber dispatch."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="float32"),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        driver._subscriptions.subscribe([], cb, driver._resolve_ioa)

        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.SPONTANEOUS,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=42.0, quality=QualityFlag(0)),
            ],
        )

        driver._on_asdu_received(asdu)
        # Dispatch is async (ensure_future), let it run.
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].point_id == "p1"
        assert received[0].value == 42.0

    async def test_interrogation_cot_dispatches(self) -> None:
        """ASDU with COT=INTERROGATED_BY_STATION triggers subscriber dispatch."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="float32"),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        driver._subscriptions.subscribe([], cb, driver._resolve_ioa)

        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.INTERROGATED_BY_STATION,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=99.0, quality=QualityFlag(0)),
            ],
        )
        driver._on_asdu_received(asdu)

        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].value == 99.0

    async def test_quality_bad(self) -> None:
        """QualityFlag.IV → Quality.BAD."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        driver._subscriptions.subscribe([], cb, driver._resolve_ioa)

        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.SPONTANEOUS,
            common_address=1,
            objects=[
                MeasuredValueShort(
                    ioa=100,
                    value=0.0,
                    quality=QualityFlag.IV,
                ),
            ],
        )
        driver._on_asdu_received(asdu)

        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].quality == Quality.BAD

    async def test_unknown_ioa_skipped(self) -> None:
        """IOA not in mapping → PointValue not generated."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        driver._subscriptions.subscribe([], cb, driver._resolve_ioa)

        # ASDU with IOA 999 (not mapped).
        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.SPONTANEOUS,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=999, value=1.0, quality=QualityFlag(0)),
            ],
        )
        driver._on_asdu_received(asdu)

        await asyncio.sleep(0.05)
        assert len(received) == 0

    async def test_single_point_information(self) -> None:
        """SinglePoint (M_SP_NA_1) dispatch works."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("sp1", 50, data_type="bool"),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        driver._subscriptions.subscribe([], cb, driver._resolve_ioa)

        asdu = ASDU(
            type_id=TypeID.M_SP_NA_1,
            cause=CauseOfTransmission.SPONTANEOUS,
            common_address=1,
            objects=[
                SinglePoint(ioa=50, value=True, quality=QualityFlag(0)),
            ],
        )
        driver._on_asdu_received(asdu)

        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].point_id == "sp1"
        assert received[0].value is True
