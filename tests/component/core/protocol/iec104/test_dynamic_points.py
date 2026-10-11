"""IEC104 动态点（显式 IOA，不登记 PointTable）镜像/写入/主动读测试。

c104 Station/Point 以最小 fake 替代；覆盖动态接收关联、身份重标、
类型兼容检查与同 IOA 多逻辑点隔离。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application import ConfigError, ProtocolCapabilityError, ProtocolError, ProtocolWrite
from core.application.protocol_contract import ProtocolSample, Quality, WriteOutcome
from core.infrastructure.protocol.iec104.config import IEC104Config
from core.infrastructure.protocol.iec104.driver import IEC104Driver, _SubscriptionRegistry
from core.infrastructure.protocol.iec104.mapping import IEC104Point, iec104_point


class _C104Point:
    """最小 c104 Point fake：记录 read/transmit，持有 type 与回调。"""

    def __init__(self, point_type: Any, *, accepted: bool = True) -> None:
        self.type = point_type
        self.accepted = accepted
        self.read_calls = 0
        self.transmit_calls: list[Any] = []
        self.value: Any = None
        self.receive_callback: Any = None

    def read(self) -> bool:
        self.read_calls += 1
        return self.accepted

    def transmit(self, cot: Any) -> bool:
        self.transmit_calls.append(cot)
        return self.accepted

    def on_receive(self, *, callable_: Any = None, **kwargs: Any) -> None:
        self.receive_callback = callable_ or kwargs.get("callable")


class _Station:
    def __init__(self) -> None:
        self.points: dict[int, _C104Point] = {}
        self.add_calls: list[tuple[int, Any]] = []

    def get_point(self, ioa: int) -> _C104Point | None:
        return self.points.get(ioa)

    def add_point(self, *, io_address: int, type: Any) -> _C104Point:  # noqa: A002
        self.add_calls.append((io_address, type))
        point = _C104Point(type)
        self.points[io_address] = point
        return point


def _driver(
    station: _Station | None = None,
    *,
    points_by_id: dict[str, IEC104Point] | None = None,
) -> IEC104Driver:
    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._closed = False
    driver._station = station if station is not None else _Station()
    driver._points_by_id = points_by_id or {}
    driver._points_by_ioa = {p.ioa: p for p in driver._points_by_id.values()}
    driver._dynamic_points = {}
    driver._command_locks = {}
    driver._active_reads = {}
    driver._active_read_locks = {}
    driver._receive_callback_factory = None
    driver._subscriptions = _SubscriptionRegistry()

    driver._config = IEC104Config(host="192.0.2.30", port=2404, t1=0.2)
    return driver


def _sample(point_id: str, value: object, *, ioa_ts: bool = False) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=value,  # type: ignore[arg-type]
        quality=Quality.GOOD,
        timestamp=datetime.now(UTC),
        timestamp_source="device" if ioa_ts else "local",
    )


# ---------------------------------------------------------------------------
# iec104_point 工厂校验
# ---------------------------------------------------------------------------


class TestIEC104PointFactory:
    def test_normalizes_type_id(self) -> None:
        point = iec104_point("d", ioa=100, type_id=" m_me_nc_1 ")
        assert point.ioa == 100
        assert point.type_id == "M_ME_NC_1"

    def test_rejects_invalid_ioa(self) -> None:
        with pytest.raises(ConfigError, match="ioa must be an integer"):
            iec104_point("d", ioa=True)
        with pytest.raises(ConfigError, match="ioa must be in 0.."):
            iec104_point("d", ioa=0x1000000)
        with pytest.raises(ConfigError, match="ioa must be an integer"):
            iec104_point("d", ioa="100")  # type: ignore[arg-type]

    def test_rejects_empty_type_id(self) -> None:
        with pytest.raises(ConfigError, match="type_id must be a non-empty string"):
            iec104_point("d", ioa=1, type_id="  ")


# ---------------------------------------------------------------------------
# 镜像读取
# ---------------------------------------------------------------------------


class TestDynamicSingleRead:
    @pytest.mark.asyncio
    async def test_dynamic_remote_read_waits_for_reply(self) -> None:
        driver = _driver()
        point = iec104_point("dyn", ioa=500, type_id="M_ME_NC_1")

        async def send(mapped: IEC104Point) -> None:
            assert mapped.ioa == 500

        driver._send_read = send  # type: ignore[method-assign]  # 测试替身替换驱动私有方法
        task = asyncio.create_task(driver.read_one(point))
        await asyncio.sleep(0)
        driver._active_reads[500].set_result(_sample("canonical", 3.5))
        result = await task
        assert result.point_id == "dyn"
        assert result.value == 3.5

    @pytest.mark.asyncio
    async def test_batch_read_is_not_supported(self) -> None:
        driver = _driver()
        with pytest.raises(ProtocolCapabilityError, match="read_many"):
            await driver.read_many(("reg",))

    @pytest.mark.asyncio
    async def test_closed_connection_rejects_dynamic_read(self) -> None:
        driver = _driver()
        driver._is_open = False
        with pytest.raises(ProtocolError, match="OPEN connection"):
            await driver.read_one(iec104_point("dyn", ioa=500))


# ---------------------------------------------------------------------------
# 动态写入（遥控/设点）
# ---------------------------------------------------------------------------


class TestDynamicWrite:
    @pytest.mark.asyncio
    async def test_dynamic_command_creates_c104_point_and_transmits_once(self) -> None:
        import c104

        station = _Station()
        driver = _driver(station)

        result = await driver.write_one(iec104_point("switch", ioa=1001, type_id="C_SC_NA_1"), True)

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert result.point_id == "switch"
        assert station.add_calls == [(1001, c104.Type.C_SC_NA_1)]
        command_point = station.points[1001]
        assert len(command_point.transmit_calls) == 1
        assert command_point.value is True

    @pytest.mark.asyncio
    async def test_existing_compatible_c104_point_is_reused(self) -> None:
        import c104

        station = _Station()
        existing = station.add_point(io_address=1001, type=c104.Type.C_SC_NA_1)
        station.add_calls.clear()
        driver = _driver(station)

        result = await driver.write_one(
            iec104_point("switch", ioa=1001, type_id="C_SC_NA_1"), False
        )

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert station.add_calls == []
        assert len(existing.transmit_calls) == 1

    @pytest.mark.asyncio
    async def test_incompatible_existing_c104_point_is_not_modified(self) -> None:
        import c104

        station = _Station()
        existing = station.add_point(io_address=1001, type=c104.Type.M_ME_NC_1)
        driver = _driver(station)

        result = await driver.write_one(iec104_point("switch", ioa=1001, type_id="C_SC_NA_1"), True)

        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert "already registered" in (result.message or "")
        assert existing.transmit_calls == []

    @pytest.mark.asyncio
    async def test_invalid_command_value_rejected_before_send(self) -> None:
        station = _Station()
        driver = _driver(station)

        result = await driver.write_one(iec104_point("switch", ioa=1001, type_id="C_SC_NA_1"), 1)

        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert station.add_calls == []

    @pytest.mark.asyncio
    async def test_missing_type_id_rejected(self) -> None:
        driver = _driver()
        result = await driver.write_one(iec104_point("switch", ioa=1001), True)
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert "type_id" in (result.message or "")

    @pytest.mark.asyncio
    async def test_monitoring_type_rejected_as_command(self) -> None:
        driver = _driver()
        result = await driver.write_one(iec104_point("switch", ioa=1001, type_id="M_ME_NC_1"), True)
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert "unsupported command type" in (result.message or "")

    @pytest.mark.asyncio
    async def test_protocol_write_form_still_works(self) -> None:
        station = _Station()
        driver = _driver(station, points_by_id={"reg": IEC104Point("reg", 1001, "C_SC_NA_1")})
        result = await driver.write_one(ProtocolWrite("reg", True))
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        with pytest.raises(TypeError, match="does not take a separate value"):
            await driver.write_one(ProtocolWrite("reg", True), False)


# ---------------------------------------------------------------------------
# 主动读取
# ---------------------------------------------------------------------------


class TestDynamicActiveRead:
    @pytest.mark.asyncio
    async def test_registers_monitoring_point_for_remote_read(self) -> None:
        import c104

        station = _Station()
        driver = _driver(station)
        point = iec104_point("dyn", ioa=700, type_id="M_ME_NC_1")

        await driver._send_read(point)
        assert station.add_calls == [(700, c104.Type.M_ME_NC_1)]
        assert station.points[700].read_calls == 1
        assert 700 in driver._dynamic_points

    @pytest.mark.asyncio
    async def test_requires_monitoring_type_for_unregistered_point(self) -> None:
        driver = _driver()
        with pytest.raises(ProtocolError, match="monitoring type_id"):
            await driver._send_read(iec104_point("dyn", ioa=700))
        assert not driver._station.add_calls

    @pytest.mark.asyncio
    async def test_remote_read_reuses_registered_c104_point(self) -> None:
        import c104

        station = _Station()
        existing = station.add_point(io_address=700, type=c104.Type.M_ME_NC_1)
        station.add_calls.clear()
        driver = _driver(station)
        await driver._send_read(iec104_point("dyn", ioa=700))
        assert station.add_calls == []
        assert existing.read_calls == 1


# ---------------------------------------------------------------------------
# 接收回调与镜像
# ---------------------------------------------------------------------------


class TestDynamicReceive:
    def test_new_point_callback_covers_dynamic_ioa(self) -> None:
        driver = _driver()
        driver._dynamic_points[500] = IEC104Point("dyn", 500, "M_ME_NC_1")
        attached: list[int] = []

        class _FakeC104Point:
            def on_receive(self, *, callable: Any) -> None:  # noqa: A002
                attached.append(500)

        class _FakeStation:
            def get_point(self, ioa: int) -> Any:
                return _FakeC104Point()

        factory_calls: list[Any] = []

        def factory(handler: Any) -> Any:
            factory_calls.append(handler)
            return handler

        driver._receive_callback_factory = factory
        driver._handle_new_point(_FakeStation(), 500, object())
        assert attached == [500]

    def test_new_point_callback_still_ignores_unknown_ioa(self) -> None:
        driver = _driver()
        driver._receive_callback_factory = lambda h: h

        class _FakeStation:
            def get_point(self, ioa: int) -> Any:
                raise AssertionError("must not attach for unknown IOA")

        driver._handle_new_point(_FakeStation(), 999, object())

    @pytest.mark.asyncio
    async def test_point_receive_uses_dynamic_identity(self) -> None:
        driver = _driver()
        driver._dynamic_points[500] = IEC104Point("dyn", 500, None)
        driver._loop = asyncio.get_running_loop()

        class _FakeC104Point:
            io_address = 500
            value = 1.25
            quality = None
            recorded_at = None

        import c104

        class _FakeMessage:
            cot = c104.Cot.SPONTANEOUS
            is_negative = False

        observed: list[ProtocolSample] = []
        arrived = asyncio.Event()

        async def callback(sample: ProtocolSample) -> None:
            observed.append(sample)
            arrived.set()

        handle = driver._subscriptions.subscribe((500,), callback)
        result = driver._handle_point_receive(_FakeC104Point(), _FakeMessage())
        assert result == c104.ResponseState.NONE
        await asyncio.wait_for(arrived.wait(), timeout=1)
        await handle.close()
        assert len(observed) == 1
        assert observed[0].point_id == "dyn"
        assert observed[0].value == 1.25
        assert observed[0].quality == Quality.UNCERTAIN
