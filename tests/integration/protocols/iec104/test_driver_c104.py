"""IEC104Driver（hat 主站）× c104/lib60870 从站的跨实现互操作集成测试。

被测组件是 ``src/core/infrastructure/protocol/iec104/driver.py``；对端是
真实 c104 loopback（IEC104MockServer）与 TCP 黑洞从站，不 mock
任何协议栈，上下行都是真实 TCP + APCI + ASDU 字节。

覆盖契约：
- 主动读 = 真实 C_RD_NA_1 请求-响应关联，不读任何本地镜像/缓存；
- listen 是纯本地注册：接收总召响应与 spontaneous 上送，不向从站注册，
  连接中不隐式总召；
- Driver 内不自动重连：从站重启后由上层显式 connect() 重建会话；
- 未配置 (CA,IOA) 的上送绝不投递业务回调；
- 品质位 / CP56Time2a 时标映射；点表热更新；停机清理。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    InterrogationRequest,
    ProtocolSample,
    Quality,
)
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.iec104_blackhole import StartdtBlackhole
from tests.support.process import free_port
from tests.support.units import NONE_UNIT
from tests.support.wait import wait_until

pytestmark = pytest.mark.real_service

DATA_POINTS = {100: 1500.5, 200: 50.0}


def _options(**extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "common_addresses": "1",
        "t0": 5.0,
        "t1": 3.0,
        "t2": 1.0,
        "t3": 30.0,
    }
    options.update(extra)
    return options


def _point(
    point_id: str,
    ioa: int,
    *,
    ca: int = 1,
    access: PointAccess = PointAccess.READ,
    type_id: str | None = None,
) -> Point:
    ext: dict[str, str | int] = {"ioa": ioa, "ca": ca}
    if type_id is not None:
        ext["type_id"] = type_id
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


def _table(points: Sequence[Point]) -> PointTable:
    return PointTable(
        PointTableId("t1"),
        Protocol("iec104"),
        {point.point_id: point for point in points},
    )


def _driver(
    port: int,
    points: Sequence[Point],
    *,
    options: dict[str, Any] | None = None,
) -> IEC104Driver:
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        _table(points),
        options or _options(),
    )


def _default_points() -> list[Point]:
    return [
        _point("meas.power", 100, type_id="M_ME_NC_1"),
        _point("meas.freq", 200, type_id="M_ME_NC_1"),
    ]


@pytest.fixture
async def server() -> AsyncIterator[IEC104MockServer]:
    srv = IEC104MockServer(port=free_port())
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


@pytest.fixture
async def driver(server: IEC104MockServer) -> AsyncIterator[IEC104Driver]:
    drv = _driver(server.port, _default_points())
    try:
        yield drv
    finally:
        await drv.close()


class _Collector:
    """listen 回调收集器。"""

    def __init__(self, *point_ids: str) -> None:
        self.samples: list[ProtocolSample] = []
        self.point_ids = point_ids
        self._seen: dict[str, asyncio.Event] = {}

    async def __call__(self, samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            self.samples.append(sample)
            event = self._seen.get(sample.point_id)
            if event is not None:
                event.set()

    def wait_for(self, point_id: str, predicate: Any = None) -> asyncio.Future[ProtocolSample]:
        event = asyncio.Event()
        self._seen[point_id] = event

        async def _poll() -> ProtocolSample:
            while True:
                for sample in reversed(self.samples):
                    if sample.point_id == point_id and (predicate is None or predicate(sample)):
                        return sample
                await asyncio.sleep(0.02)

        return asyncio.ensure_future(_poll())


# ===========================================================================
# 建连、主动读与能力边界
# ===========================================================================


class TestConnectAndActiveRead:
    async def test_connect_reports_healthy(self, driver: IEC104Driver) -> None:
        await driver.connect()
        assert (await driver.health()).healthy is True

    async def test_read_one_is_a_real_c_rd_transaction(self, driver: IEC104Driver) -> None:
        """不做总召、不依赖缓存：直接 C_RD 拿到本次响应。"""
        await driver.connect()
        sample = await driver.read_one("meas.power")
        assert sample.point_id == "meas.power"
        assert sample.value == pytest.approx(1500.5, abs=1e-4)
        assert sample.quality is Quality.GOOD
        assert sample.timestamp_source == "local"  # M_ME_NC_1 无类型时标

    async def test_read_one_returns_fresh_value_not_stale_cache(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        await driver.connect()
        first = await driver.read_one("meas.power")
        assert first.value == pytest.approx(1500.5, abs=1e-4)
        await server.set_value(100, 1600.25)
        second = await driver.read_one("meas.power")
        assert second.value == pytest.approx(1600.25, abs=1e-4)

    async def test_read_many_is_not_supported(self, driver: IEC104Driver) -> None:
        await driver.connect()
        with pytest.raises(ProtocolCapabilityError, match="batch read"):
            await driver.read_many(["meas.freq", "meas.power"])

    async def test_subscribe_is_not_supported(self, driver: IEC104Driver) -> None:
        await driver.connect()

        async def _cb(_samples: tuple[ProtocolSample, ...]) -> None:
            return None

        with pytest.raises(ProtocolCapabilityError, match="subscription"):
            await driver.subscribe(["meas.power"], _cb)

    async def test_read_unknown_point_raises(self, driver: IEC104Driver) -> None:
        await driver.connect()
        with pytest.raises(ConfigError, match="ghost.point"):
            await driver.read_one("ghost.point")

    async def test_read_without_connect_raises(self, driver: IEC104Driver) -> None:
        with pytest.raises(ProtocolError, match="session unavailable"):
            await driver.read_one("meas.power")

    async def test_listen_without_connect_raises(self, driver: IEC104Driver) -> None:
        with pytest.raises(ProtocolError, match="session unavailable"):

            async def _cb(_samples: tuple[ProtocolSample, ...]) -> None:
                return None

            await driver.listen(["meas.power"], _cb)


# ===========================================================================
# 总召 + 本地监听
# ===========================================================================


class TestInterrogateAndListen:
    async def test_interrogate_delivers_both_points_to_listener(self, driver: IEC104Driver) -> None:
        await driver.connect()
        collected = _Collector()
        await driver.listen((), collected)
        await driver.interrogate(InterrogationRequest())  # 显式总召，阻塞到 ACT_TERM
        await asyncio.wait_for(collected.wait_for("meas.freq"), timeout=5.0)
        values = {s.point_id: s.value for s in collected.samples}
        assert values["meas.power"] == pytest.approx(1500.5, abs=1e-4)
        assert values["meas.freq"] == pytest.approx(50.0, abs=1e-4)

    async def test_listener_filter_applies(self, driver: IEC104Driver) -> None:
        await driver.connect()
        collected = _Collector()
        await driver.listen(("meas.power",), collected)
        await driver.interrogate(InterrogationRequest())
        await asyncio.sleep(0.5)
        assert {s.point_id for s in collected.samples} == {"meas.power"}

    async def test_spontaneous_value_reaches_listener(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        await driver.connect()
        collected = _Collector()
        await driver.listen(("meas.power",), collected)
        await server.set_value(100, 999.0, spontaneous=True)
        sample = await asyncio.wait_for(
            collected.wait_for("meas.power", lambda s: s.value == pytest.approx(999.0, abs=1e-4)),
            timeout=5.0,
        )
        assert sample.quality is Quality.GOOD

    async def test_unmapped_address_is_never_delivered(self) -> None:
        srv = IEC104MockServer(
            port=free_port(),
            stations={1: {100: 1500.5, 999: 7.0}},
        )
        await srv.start()
        drv = _driver(srv.port, [_point("meas.power", 100)])
        received: list[ProtocolSample] = []

        async def cb(samples: tuple[ProtocolSample, ...]) -> None:
            for sample in samples:
                received.append(sample)

        try:
            await drv.connect()
            await drv.listen((), cb)
            await srv.set_value(999, 8.0, spontaneous=True)
            await asyncio.sleep(0.5)
            assert all(s.point_id == "meas.power" for s in received)
        finally:
            await drv.close()
            await srv.stop()

    async def test_listen_close_stops_delivery(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        await driver.connect()
        collected = _Collector()
        handle = await driver.listen(("meas.power",), collected)
        await driver.interrogate(InterrogationRequest())
        await asyncio.wait_for(collected.wait_for("meas.power"), timeout=5.0)
        await handle.close()

        await handle.close()  # 幂等

        count_after_close = len(collected.samples)
        await server.set_value(100, 777.0, spontaneous=True)
        await asyncio.sleep(0.5)
        assert len(collected.samples) == count_after_close

    async def test_driver_close_drops_late_uploads(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """close 后对端继续上送：不得投递业务回调，也不得抛异常。"""
        await driver.connect()
        received: list[ProtocolSample] = []

        async def cb(samples: tuple[ProtocolSample, ...]) -> None:
            for sample in samples:
                received.append(sample)

        await driver.listen((), cb)
        await driver.close()
        await server.set_value(100, 555.0, spontaneous=True)
        await asyncio.sleep(0.5)
        assert not any(s.value == pytest.approx(555.0, abs=1e-4) for s in received)


# ===========================================================================
# 多 CA：一条 TCP，完整 (CA,IOA) 寻址
# ===========================================================================


class TestMultipleCommonAddresses:
    async def test_same_ioa_on_different_cas_routed_independently(self) -> None:
        srv = IEC104MockServer(
            port=free_port(),
            stations={1: {100: 11.1}, 2: {100: 22.2}},
        )
        await srv.start()
        points = [
            _point("ca1.power", 100, ca=1, type_id="M_ME_NC_1"),
            _point("ca2.power", 100, ca=2, type_id="M_ME_NC_1"),
        ]
        drv = _driver(srv.port, points, options=_options(common_addresses="1,2"))
        try:
            await drv.connect()
            s1 = await drv.read_one("ca1.power")
            s2 = await drv.read_one("ca2.power")
            assert s1.value == pytest.approx(11.1, abs=1e-4)
            assert s2.value == pytest.approx(22.2, abs=1e-4)

            # 默认总召逐 CA 执行；两个 CA 的数据都投递给监听者。
            collected = _Collector()
            await drv.listen((), collected)
            await drv.interrogate(InterrogationRequest())
            await asyncio.sleep(0.5)
            by_ca = {s.point_id for s in collected.samples}
            assert by_ca == {"ca1.power", "ca2.power"}
        finally:
            await drv.close()
            await srv.stop()

    async def test_interrogate_explicit_unknown_ca_rejected(self) -> None:
        srv = IEC104MockServer(port=free_port(), stations={1: {100: 1.0}, 2: {100: 2.0}})
        await srv.start()
        drv = _driver(
            srv.port,
            [_point("ca1", 100, ca=1), _point("ca2", 100, ca=2)],
            options=_options(common_addresses="1,2"),
        )
        try:
            await drv.connect()
            with pytest.raises(ConfigError, match="not configured"):
                await drv.interrogate(InterrogationRequest(common_address=3))
        finally:
            await drv.close()
            await srv.stop()


# ===========================================================================
# 从站重启：驱动不自动重连，上层显式重建会话
# ===========================================================================


class TestServerRestart:
    async def test_server_restart_requires_explicit_reconnect(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        await driver.connect()
        sample = await driver.read_one("meas.power")
        assert sample.value == pytest.approx(1500.5, abs=1e-4)

        await server.stop()
        await wait_until(
            lambda: True if not driver.is_open() else None,
            timeout=10.0,
            description="driver detected disconnect",
        )
        with pytest.raises(ProtocolError, match="session unavailable|connection lost"):
            await driver.read_one("meas.power")

        await server.start()
        with pytest.raises(ProtocolError):
            await driver.read_one("meas.power")  # 旧会话不会自愈
        # 显式重建会话必须先收敛旧连接（与 Watchdog close()→connect() 契约一致）。
        await driver.close()
        await driver.connect()
        assert driver.is_open()
        sample = await driver.read_one("meas.power")
        assert sample.value == pytest.approx(1500.5, abs=1e-4)

    async def test_connect_failure_then_retry_succeeds(self) -> None:
        port = free_port()
        drv = _driver(port, _default_points(), options=_options(t0=2.0))
        try:
            with pytest.raises(ProtocolError, match=r"(?i)connect.*failed|timed out"):
                await drv.connect()

            srv = IEC104MockServer(port=port)
            await srv.start()
            try:
                await drv.connect()
                assert (await drv.health()).healthy is True
            finally:
                await srv.stop()
        finally:
            await drv.close()


# ===========================================================================
# 值语义：品质位 / 时标 / 点表
# ===========================================================================


class TestValueSemantics:
    async def test_bad_quality_mapping(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        import c104

        await driver.connect()
        collected = _Collector()
        await driver.listen(("meas.power",), collected)
        await server.set_value(100, 1.0, quality=c104.Quality.Invalid)
        await server.set_value(100, 1.0, spontaneous=True)
        sample = await asyncio.wait_for(
            collected.wait_for(
                "meas.power",
                lambda s: s.quality is Quality.BAD and s.value == pytest.approx(1.0, abs=1e-4),
            ),
            timeout=5.0,
        )
        assert sample.quality is Quality.BAD

    async def test_timestamp_short_fallback_is_local_receive_time(self) -> None:
        """M_ME_NC_1 不携带时标 → 接收时刻本地生成（timestamp_source=local）。"""
        srv = IEC104MockServer(port=free_port(), data_points={300: 42.0})
        await srv.start()
        drv = _driver(srv.port, [_point("meas.value", 300, type_id="M_ME_NC_1")])
        try:
            await drv.connect()
            before = datetime.now(UTC)
            sample = await drv.read_one("meas.value")
            after = datetime.now(UTC)
            assert sample.timestamp_source == "local"
            assert before <= sample.timestamp <= after
        finally:
            await drv.close()
            await srv.stop()

    async def test_update_point_table(self, driver: IEC104Driver) -> None:
        """连接中热更点表：旧 point_id 立即失效；新 IOA 主动读立即可用。"""
        await driver.connect()
        sample = await driver.read_one("meas.power")
        assert sample.value == pytest.approx(1500.5, abs=1e-4)

        driver.update_point_table(_table([_point("meas.only_freq", 200, type_id="M_ME_NC_1")]))
        with pytest.raises(ConfigError, match="meas.power"):
            await driver.read_one("meas.power")
        sample = await driver.read_one("meas.only_freq")
        assert sample.value == pytest.approx(50.0, abs=1e-4)


# ===========================================================================
# 生命周期清理
# ===========================================================================


class TestLifecycle:
    async def test_close_disconnects_and_rejects_read(self, driver: IEC104Driver) -> None:
        await driver.connect()
        await driver.close()
        assert driver.is_open() is False
        with pytest.raises(ProtocolError, match="session unavailable"):
            await driver.read_one("meas.power")

    async def test_close_idempotent(self, driver: IEC104Driver) -> None:
        await driver.connect()
        await driver.close()
        await driver.close()
        assert driver.is_open() is False

    async def test_close_without_connect(self) -> None:
        drv = _driver(free_port(), _default_points())
        await drv.close()
        assert drv.is_open() is False

    async def test_pending_read_fails_on_close(self) -> None:
        """对端不应答 C_RD 时 close：在途读取以连接错误失败，不挂起。"""
        blackhole = StartdtBlackhole(free_port())
        await blackhole.start()
        drv = _driver(
            blackhole.port,
            _default_points(),
            options=_options(t1=10.0),
        )
        try:
            await drv.connect()
            pending = asyncio.create_task(drv.read_one("meas.power"))
            await asyncio.sleep(0.1)
            assert not pending.done()
            await drv.close()
            with pytest.raises(ProtocolError):
                await pending
        finally:
            await drv.close()
            await blackhole.stop()
