"""IEC104Driver × c104 集成契约测试矩阵。

被测组件是 ``src/core/infrastructure/protocol/iec104/driver.py`` 的共享 Core
IEC104Driver；对端是真实 c104 loopback（IEC104MockServer /
IEC104ControlServer），不 mock c104。

覆盖：建连/健康、显式总召填镜像、自动重连与补召、建连失败重试、spontaneous
分发、命令否定确认/断线/超时、多订阅与订阅生命周期、品质位与 CP56Time2a
时标映射、点表热更新、停机清理。

与旧栈（wind_hub_core）语义差异：
- 新 Driver 建连后不做隐式总召（init=NONE），需要镜像数据时显式
  ``interrogate()``；持续采集与补召策略属于上层应用职责。
- 重复 IOA 在 ``build_iec104_index`` 直接拒绝（ConfigError），旧栈
  “last wins” 用例不再存在；拒绝路径由 unit 测试覆盖。
- 未寻址 point_id 的 read/write 抛 ConfigError，不再返回 BAD 样本或
  success=False 结果。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application import ConfigError, ProtocolError
from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    Quality,
)
from core.domain import (
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.process import free_port
from tests.support.wait import wait_until

pytestmark = pytest.mark.real_service

# 与 IEC104MockServer 默认数据点一致。
DATA_POINTS = {100: 1500.5, 200: 50.0}


def _options(*, t1: float = 3.0) -> dict[str, float | int]:
    return {
        "common_addr": 1,
        "t0": 5.0,
        "t1": t1,
        "t2": 1.0,
        "t3": 30.0,
    }


def _point(
    point_id: str,
    ioa: int,
    *,
    access: PointAccess = PointAccess.READ,
    type_id: str | None = None,
) -> Point:
    ext: dict[str, str | int] = {"ioa": ioa}
    if type_id is not None:
        ext["type_id"] = type_id
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=access,
        ext=ext,
    )


def _table(points: Sequence[Point]) -> PointTable:
    return PointTable(
        "t1",
        Protocol("iec104"),
        {point.point_id: point for point in points},
    )


def _driver(
    port: int,
    points: Sequence[Point],
    *,
    t1: float = 3.0,
) -> IEC104Driver:
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        _table(points),
        _options(t1=t1),
    )


def _default_points() -> list[Point]:
    return [_point("meas.power", 100), _point("meas.freq", 200)]


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


async def _read_value(driver: IEC104Driver, point_id: str) -> Any:
    """轮询读直到拿到 GOOD 镜像值（总召/spontaneous 到达是异步的）。"""

    async def _probe() -> Any:
        sample = await driver.read_one(point_id)
        return sample.value if sample.quality is Quality.GOOD else None

    return await wait_until(
        _probe, timeout=10.0, description=f"mirror filled for {point_id}"
    )


# ===========================================================================
# 建连与总召
# ===========================================================================


class TestConnectAndRead:
    async def test_connect_reports_healthy(self, driver: IEC104Driver) -> None:
        await driver.connect()
        assert driver.health().healthy is True

    async def test_general_interrogation_fills_mirror(
        self, driver: IEC104Driver
    ) -> None:
        await driver.connect()
        await driver.interrogate()
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)
        assert await _read_value(driver, "meas.freq") == pytest.approx(50.0)

    async def test_read_many_preserves_request_order(
        self, driver: IEC104Driver
    ) -> None:
        await driver.connect()
        await driver.interrogate()
        await _read_value(driver, "meas.power")
        samples = await driver.read_many(["meas.freq", "meas.power", "meas.freq"])
        assert [sample.point_id for sample in samples] == [
            "meas.freq",
            "meas.power",
            "meas.freq",
        ]
        assert all(sample.quality is Quality.GOOD for sample in samples)

    async def test_read_unknown_point_raises(self, driver: IEC104Driver) -> None:
        """未寻址 point_id 属于配置错误，直接抛 ConfigError。"""
        await driver.connect()
        with pytest.raises(ConfigError, match="ghost.point"):
            await driver.read_one("ghost.point")

    async def test_read_without_connect_raises(self, driver: IEC104Driver) -> None:
        with pytest.raises(ProtocolError, match="OPEN connection"):
            await driver.read_one("meas.power")


# ===========================================================================
# 重连与建连失败
# ===========================================================================


class TestReconnect:
    async def test_reconnect_after_server_restart_refills_mirror(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """从站重启后 c104 自动重连；断线清空镜像，显式补召后恢复可读。"""
        received: list[ProtocolSample] = []

        async def cb(sample: ProtocolSample) -> None:
            received.append(sample)

        await driver.subscribe([], cb)
        await driver.connect()
        await driver.interrogate()
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)

        await server.stop()
        await wait_until(
            lambda: True if not driver.health().healthy else None,
            timeout=10.0,
            description="driver detected disconnect",
        )
        # 断线后 read 立即拒绝，不返回陈旧镜像值。
        with pytest.raises(ProtocolError, match="OPEN connection"):
            await driver.read_one("meas.power")

        await server.start()
        await wait_until(
            lambda: True if driver.health().healthy else None,
            timeout=10.0,
            description="driver auto-reconnected",
        )
        # 重连不隐式总召：上层显式补召后镜像恢复 GOOD 值。
        await driver.interrogate()
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)

    async def test_connect_failure_then_retry_succeeds(self) -> None:
        """首次建连超时抛 ProtocolError；对端上线后重试 connect 成功。"""
        port = free_port()
        drv = _driver(port, _default_points(), t1=1.0)
        try:
            with pytest.raises(ProtocolError, match="timed out"):
                await drv.connect()

            srv = IEC104MockServer(port=port)
            await srv.start()
            try:
                await drv.connect()
                assert drv.health().healthy is True
            finally:
                await srv.stop()
        finally:
            await drv.close()


# ===========================================================================
# spontaneous 分发
# ===========================================================================


class TestSpontaneous:
    async def test_spontaneous_value_reaches_subscriber(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        received: list[ProtocolSample] = []

        async def cb(sample: ProtocolSample) -> None:
            received.append(sample)

        await driver.subscribe(["meas.power"], cb)
        await driver.connect()
        await driver.interrogate()
        await _read_value(driver, "meas.power")

        await server.set_value(100, 999.0, spontaneous=True)
        await wait_until(
            lambda: [s for s in received if s.value == pytest.approx(999.0)]
            or None,
            timeout=10.0,
            description="spontaneous value dispatched",
        )
        sample = received[-1]
        assert sample.point_id == "meas.power"
        assert sample.quality is Quality.GOOD


# ===========================================================================
# 命令失败路径
# ===========================================================================


@pytest.fixture
async def control_server() -> AsyncIterator[IEC104ControlServer]:
    srv = IEC104ControlServer(
        common_addr=1, data_points={100: 1500.5}, reject_ioas={1001}
    )
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _switch_point(point_id: str = "switch.on", ioa: int = 1001) -> Point:
    return _point(
        point_id,
        ioa,
        access=PointAccess.WRITE,
        type_id="C_SC_NA_1",
    )


class TestCommandFailures:
    async def test_negative_command_confirmation(
        self, control_server: IEC104ControlServer
    ) -> None:
        """从站否定确认（ACT_CON negative）→ success=False。"""
        drv = _driver(control_server.port, [_switch_point()])
        try:
            await drv.connect()
            result = await drv.write_one(
                ProtocolWrite(point_id="switch.on", value=True)
            )
        finally:
            await drv.close()

        assert result.success is False
        assert "negative" in (result.message or "")

    async def test_command_interrupted_by_disconnect(
        self, control_server: IEC104ControlServer
    ) -> None:
        """命令在途期间对端断开 → 该命令 success=False，driver 不挂起。"""
        drv = _driver(control_server.port, [_switch_point()])
        try:
            await drv.connect()
            task = asyncio.create_task(
                drv.write_one(ProtocolWrite(point_id="switch.on", value=True))
            )
            await asyncio.sleep(0.05)
            await control_server.stop()
            result = await asyncio.wait_for(task, timeout=15.0)
        finally:
            await drv.close()

        assert result.success is False
        assert result.message

    async def test_write_after_detected_disconnect_raises(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """断线已被感知后 write 立即抛 ProtocolError（不假装排队）。"""
        await driver.connect()
        await server.stop()
        await wait_until(
            lambda: True if not driver.health().healthy else None,
            timeout=10.0,
            description="driver detected disconnect",
        )
        with pytest.raises(ProtocolError, match="OPEN connection"):
            await driver.write_one(ProtocolWrite(point_id="meas.power", value=1.0))


class _StartdtBlackhole:
    """只维持传输层存活的 TCP 假从站：应答 STARTDT、对每个 I 帧回 S 帧
    传输确认，但从不给应用层响应（ACT_CON）。

    用于驱动命令超时路径：连接保持 OPEN，命令在 command_timeout 后失败。
    S 帧只携带接收序号（传输层确认语义），不含任何 ASDU 处理。
    """

    _STARTDT_ACT = bytes.fromhex("680407000000")
    _STARTDT_CON = bytes.fromhex("68040b000000")

    def __init__(self, port: int) -> None:
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._recv_seq = 0

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            while True:
                data = await reader.read(255)
                if not data:
                    break
                response = self._respond(data)
                if response:
                    writer.write(response)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def _respond(self, data: bytes) -> bytes:
        """按 APCI 长度逐帧处理；只回 STARTDT_CON 与 S 帧确认。"""
        out = bytearray()
        offset = 0
        while offset + 6 <= len(data):
            if data[offset] != 0x68:
                break
            length = data[offset + 1]
            frame = data[offset : offset + 2 + length]
            if len(frame) < 2 + length:
                break
            control = frame[2:6]
            if frame == self._STARTDT_ACT:
                out += self._STARTDT_CON
            elif control[0] & 0x01 == 0:  # I 帧：S 帧传输确认，不给 ACT_CON
                self._recv_seq += 1
                out += bytes([0x68, 0x04, 0x01, 0x00])
                out += (self._recv_seq * 2).to_bytes(2, "little")
            offset += 2 + length
        return bytes(out)

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class TestCommandTimeout:
    async def test_command_timeout(self) -> None:
        """对端不应答 I 帧 → transmit 在 command_timeout（t1×2）后失败为 timeout。"""
        blackhole = _StartdtBlackhole(free_port())
        await blackhole.start()
        drv = _driver(blackhole.port, [_switch_point()], t1=1.0)
        try:
            await drv.connect()
            result = await asyncio.wait_for(
                drv.write_one(ProtocolWrite(point_id="switch.on", value=True)),
                timeout=15.0,
            )
        finally:
            await drv.close()
            await blackhole.stop()

        assert result.success is False
        assert "timeout" in (result.message or "")


# ===========================================================================
# 订阅生命周期
# ===========================================================================


class TestSubscriptions:
    async def test_multiple_subscriptions_dispatch(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """按 IOA 订阅只收到本 IOA；全局订阅收到全部。"""
        per_power: list[ProtocolSample] = []
        per_freq: list[ProtocolSample] = []
        all_values: list[ProtocolSample] = []

        async def cb_power(sample: ProtocolSample) -> None:
            per_power.append(sample)

        async def cb_freq(sample: ProtocolSample) -> None:
            per_freq.append(sample)

        async def cb_all(sample: ProtocolSample) -> None:
            all_values.append(sample)

        await driver.subscribe(["meas.power"], cb_power)
        await driver.subscribe(["meas.freq"], cb_freq)
        await driver.subscribe([], cb_all)
        await driver.connect()
        await driver.interrogate()

        await wait_until(
            lambda: all_values if len(all_values) >= 2 else None,
            timeout=10.0,
            description="global subscriber received both points",
        )
        assert {sample.point_id for sample in per_power} == {"meas.power"}
        assert {sample.point_id for sample in per_freq} == {"meas.freq"}
        assert {sample.point_id for sample in all_values} >= {
            "meas.power",
            "meas.freq",
        }

    async def test_subscription_close_stops_delivery(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        received: list[ProtocolSample] = []

        async def cb(sample: ProtocolSample) -> None:
            received.append(sample)

        handle = await driver.subscribe(["meas.power"], cb)
        await driver.connect()
        await driver.interrogate()
        await _read_value(driver, "meas.power")
        await handle.close()
        await handle.close()  # 幂等

        count_after_close = len(received)
        await server.set_value(100, 777.0, spontaneous=True)
        await asyncio.sleep(0.5)
        assert len(received) == count_after_close

    async def test_close_during_active_subscription(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """有活动订阅时 close：停机竞态下回调被丢弃，不抛异常。"""
        received: list[ProtocolSample] = []

        async def cb(sample: ProtocolSample) -> None:
            received.append(sample)

        await driver.subscribe([], cb)
        await driver.connect()
        await driver.close()

        # close 后对端继续上送：c104 回调到达已关闭 driver 必须被安全丢弃。
        await server.set_value(100, 555.0, spontaneous=True)
        await asyncio.sleep(0.5)
        assert not any(s.value == pytest.approx(555.0) for s in received)


# ===========================================================================
# 值语义：品质位 / 时标 / 点表
# ===========================================================================


class TestValueSemantics:
    async def test_bad_quality_mapping(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """从站置 Invalid 品质位 → 驱动镜像与订阅均为 Quality.BAD。"""
        import c104

        await driver.connect()
        await server.set_value(100, 1.0, quality=c104.Quality.Invalid)
        await driver.interrogate()

        async def _probe() -> ProtocolSample | None:
            sample = await driver.read_one("meas.power")
            return sample if sample.quality is Quality.BAD else None

        sample = await wait_until(
            _probe, timeout=10.0, description="BAD quality propagated"
        )
        assert sample.value == pytest.approx(1.0)

    async def test_timestamp_mapping(self) -> None:
        """带时标类型（M_ME_TF_1）的 CP56Time2a → ProtocolSample.timestamp（UTC）。

        M_ME_NC_1 等无时标类型的 ASDU 不携带 CP56Time2a，时标映射只能经
        带时标 TypeID 验证。
        """
        import c104

        srv = IEC104MockServer(
            port=free_port(),
            data_points={300: 42.0},
            point_type=c104.Type.M_ME_TF_1,
        )
        await srv.start()
        drv = _driver(srv.port, [_point("meas.tagged", 300)])
        try:
            await drv.connect()
            stamp = datetime(2026, 10, 5, 8, 30, 15, 250000, tzinfo=UTC)
            await srv.set_value(300, 43.0, recorded_at=stamp)
            await drv.interrogate()

            async def _probe() -> ProtocolSample | None:
                sample = await drv.read_one("meas.tagged")
                if sample.quality is not Quality.GOOD:
                    return None
                return sample if sample.value == pytest.approx(43.0) else None

            sample = await wait_until(
                _probe, timeout=10.0, description="timestamped value received"
            )
            assert sample.timestamp == stamp
        finally:
            await drv.close()
            await srv.stop()

    async def test_update_point_table(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """连接期间热更新点表：旧 point_id 立即失效，新 IOA 总召后可读。"""
        await driver.connect()
        await driver.interrogate()
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)

        driver.update_point_table(_table([_point("meas.only_freq", 200)]))
        with pytest.raises(ConfigError, match="meas.power"):
            await driver.read_one("meas.power")
        # 旧镜像已清空：新点在下一次总召前为 BAD。
        stale = await driver.read_one("meas.only_freq")
        assert stale.quality is Quality.BAD

        await driver.interrogate()
        assert await _read_value(driver, "meas.only_freq") == pytest.approx(50.0)


# ===========================================================================
# 生命周期清理
# ===========================================================================


class TestLifecycle:
    async def test_close_disconnects_and_rejects_read(
        self, driver: IEC104Driver
    ) -> None:
        await driver.connect()
        await driver.close()
        assert driver.health().healthy is False
        with pytest.raises(ProtocolError, match="OPEN connection"):
            await driver.read_one("meas.power")

    async def test_close_idempotent(self, driver: IEC104Driver) -> None:
        await driver.connect()
        await driver.close()
        await driver.close()
        assert driver.health().healthy is False

    async def test_close_without_connect(self) -> None:
        drv = _driver(free_port(), _default_points())
        await drv.close()
        assert drv.health().healthy is False
