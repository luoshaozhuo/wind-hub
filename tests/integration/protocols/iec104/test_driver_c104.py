"""IEC104Driver × c104 集成契约测试矩阵。

覆盖 Round 1 迁移矩阵中未被 test_iec104_real / test_remote_control 覆盖的
条目：自动重连与补召、建连失败重试、spontaneous 分发、命令否定确认/断线/
超时、多订阅与订阅生命周期、品质位与 CP56Time2a 时标映射、重复 IOA、
点表热重载、停机清理。全部经真实 c104 loopback，不 mock c104。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.process import free_port
from tests.support.wait import wait_until
from wind_hub_core.config import DeviceConfig, PointAddress, PointConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef, PointValue, Quality
from wind_hub_core.protocol.iec104.driver import IEC104Driver

pytestmark = pytest.mark.real_service

# 与 IEC104MockServer 默认数据点一致。
DATA_POINTS = {100: 1500.5, 200: 50.0}


def _device_config(port: int, *, t1: float = 3.0) -> DeviceConfig:
    return DeviceConfig(
        device_id="iec104-1",
        protocol="iec104",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=port,
            extensions={
                "common_addr": 1,
                "t0": 5.0,
                "t1": t1,
                "t2": 1.0,
                "t3": 30.0,
            },
        ),
        point_table="t1",
    )


def _point(point_id: str, ioa: int, data_type: str = "float32") -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(ioa=ioa),
        data_type=data_type,
    )


def _ref(point_id: str) -> PointRef:
    return PointRef(device_id="iec104-1", point_id=point_id)


def _default_mapping() -> list[PointConfig]:
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
    drv = IEC104Driver(_device_config(server.port))
    drv.set_points_mapping(_default_mapping())
    try:
        yield drv
    finally:
        await drv.close()


async def _read_value(driver: IEC104Driver, point_id: str) -> Any:
    """轮询读直到拿到 GOOD 缓存值（总召/spontaneous 到达是异步的）。"""
    async def _probe() -> Any:
        values = await driver.read([_ref(point_id)])
        value = values[0]
        return value.value if value.quality is Quality.GOOD else None

    return await wait_until(
        _probe, timeout=10.0, description=f"cache filled for {point_id}"
    )


# ===========================================================================
# 重连与建连
# ===========================================================================


class TestReconnect:
    async def test_reconnect_after_server_restart_refills_cache(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """从站重启后 c104 自动重连；活动订阅触发补召，镜像恢复可读。"""
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        await driver.subscribe([], cb)
        await driver.connect()
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)

        await server.stop()
        await wait_until(
            lambda: True if not driver.health().healthy else None,
            timeout=10.0,
            description="driver detected disconnect",
        )
        # 断线后 read 立即拒绝，不返回陈旧镜像值。
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([_ref("meas.power")])

        await server.start()
        await wait_until(
            lambda: True if driver.health().healthy else None,
            timeout=10.0,
            description="driver auto-reconnected",
        )
        # 补召后无需显式 interrogate 即恢复 GOOD 值。
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)

    async def test_connect_failure_then_retry_succeeds(self) -> None:
        """首次建连超时抛 ProtocolError；对端上线后重试 connect 成功。"""
        port = free_port()
        drv = IEC104Driver(_device_config(port, t1=1.0))
        drv.set_points_mapping(_default_mapping())
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
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        await driver.subscribe([_ref("meas.power")], cb)
        await driver.connect()
        await _read_value(driver, "meas.power")

        await server.set_value(100, 999.0, spontaneous=True)
        await wait_until(
            lambda: [pv for pv in received if pv.value == pytest.approx(999.0)]
            or None,
            timeout=10.0,
            description="spontaneous value dispatched",
        )
        pv = received[-1]
        assert pv.point_id == "meas.power"
        assert pv.quality is Quality.GOOD
        assert pv.source == "iec104"


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


class TestCommandFailures:
    async def test_negative_command_confirmation(
        self, control_server: IEC104ControlServer
    ) -> None:
        """从站否定确认（ACT_CON negative）→ success=False。"""
        drv = IEC104Driver(_device_config(control_server.port))
        drv.set_points_mapping([_point("switch.on", 1001, data_type="bool")])
        try:
            await drv.connect()
            results = await drv.write(
                [
                    Command(
                        command_id="neg1",
                        device_id="iec104-1",
                        point_id="switch.on",
                        value=True,
                    )
                ]
            )
        finally:
            await drv.close()

        assert results[0].success is False
        assert "negative" in (results[0].error or "")

    async def test_command_interrupted_by_disconnect(
        self, control_server: IEC104ControlServer
    ) -> None:
        """命令在途期间对端断开 → 该命令 success=False，driver 不挂起。"""
        drv = IEC104Driver(_device_config(control_server.port))
        drv.set_points_mapping([_point("switch.on", 1001, data_type="bool")])
        try:
            await drv.connect()
            task = asyncio.create_task(
                drv.write(
                    [
                        Command(
                            command_id="disc1",
                            device_id="iec104-1",
                            point_id="switch.on",
                            value=True,
                        )
                    ]
                )
            )
            await asyncio.sleep(0.05)
            await control_server.stop()
            results = await asyncio.wait_for(task, timeout=15.0)
        finally:
            await drv.close()

        assert results[0].success is False
        assert results[0].error

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
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.write(
                [
                    Command(
                        command_id="disc2",
                        device_id="iec104-1",
                        point_id="meas.power",
                        value=1.0,
                    )
                ]
            )


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
        drv = IEC104Driver(_device_config(blackhole.port, t1=1.0))
        drv.set_points_mapping([_point("switch.on", 1001, data_type="bool")])
        try:
            await drv.connect()
            results = await asyncio.wait_for(
                drv.write(
                    [
                        Command(
                            command_id="t-out",
                            device_id="iec104-1",
                            point_id="switch.on",
                            value=True,
                        )
                    ]
                ),
                timeout=15.0,
            )
        finally:
            await drv.close()
            await blackhole.stop()

        assert results[0].success is False
        assert "timeout" in (results[0].error or "")


# ===========================================================================
# 订阅生命周期
# ===========================================================================


class TestSubscriptions:
    async def test_multiple_subscriptions_dispatch(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """按 IOA 订阅只收到本 IOA；全局订阅收到全部。"""
        per_power: list[PointValue] = []
        per_freq: list[PointValue] = []
        all_values: list[PointValue] = []

        async def cb_power(pv: PointValue) -> None:
            per_power.append(pv)

        async def cb_freq(pv: PointValue) -> None:
            per_freq.append(pv)

        async def cb_all(pv: PointValue) -> None:
            all_values.append(pv)

        await driver.subscribe([_ref("meas.power")], cb_power)
        await driver.subscribe([_ref("meas.freq")], cb_freq)
        await driver.subscribe([], cb_all)
        await driver.connect()

        await wait_until(
            lambda: all_values if len(all_values) >= 2 else None,
            timeout=10.0,
            description="global subscriber received both points",
        )
        assert {pv.point_id for pv in per_power} == {"meas.power"}
        assert {pv.point_id for pv in per_freq} == {"meas.freq"}
        assert {pv.point_id for pv in all_values} >= {"meas.power", "meas.freq"}

    async def test_subscription_close_stops_delivery(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        handle = await driver.subscribe([_ref("meas.power")], cb)
        await driver.connect()
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
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        await driver.subscribe([], cb)
        await driver.connect()
        await driver.close()

        # close 后对端继续上送：c104 回调到达已关闭 driver 必须被安全丢弃。
        await server.set_value(100, 555.0, spontaneous=True)
        await asyncio.sleep(0.5)
        assert not any(pv.value == pytest.approx(555.0) for pv in received)


# ===========================================================================
# 值语义：品质位 / 时标 / 点表
# ===========================================================================


class TestValueSemantics:
    async def test_bad_quality_mapping(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """从站置 Invalid 品质位 → 驱动 read/subscribe 均为 Quality.BAD。"""
        import c104

        await driver.connect()
        await server.set_value(100, 1.0, quality=c104.Quality.Invalid)
        await driver.interrogate()

        async def _probe() -> PointValue | None:
            value = (await driver.read([_ref("meas.power")]))[0]
            return value if value.quality is Quality.BAD else None

        pv = await wait_until(
            _probe, timeout=10.0, description="BAD quality propagated"
        )
        assert pv.value == pytest.approx(1.0)

    async def test_timestamp_mapping(self) -> None:
        """带时标类型（M_ME_TF_1）的 CP56Time2a → PointValue.timestamp（UTC）。

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
        drv = IEC104Driver(_device_config(srv.port))
        drv.set_points_mapping([_point("meas.tagged", 300)])
        try:
            await drv.connect()
            stamp = datetime(2026, 10, 5, 8, 30, 15, 250000, tzinfo=UTC)
            await srv.set_value(300, 43.0, recorded_at=stamp)
            await drv.interrogate()

            async def _probe() -> PointValue | None:
                value = (await drv.read([_ref("meas.tagged")]))[0]
                if value.quality is not Quality.GOOD:
                    return None
                return value if value.value == pytest.approx(43.0) else None

            pv = await wait_until(
                _probe, timeout=10.0, description="timestamped value received"
            )
            assert pv.timestamp == stamp
        finally:
            await drv.close()
            await srv.stop()

    async def test_duplicate_ioa_last_wins(
        self, server: IEC104MockServer
    ) -> None:
        """重复 IOA 记录 warning：两个 point_id 都可读同一 IOA，
        但到达数据只归属于后注册的 point_id。"""
        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        drv = IEC104Driver(_device_config(server.port))
        drv.set_points_mapping(
            [_point("meas.first", 100), _point("meas.second", 100)]
        )
        try:
            await drv.subscribe([], cb)
            await drv.connect()
            assert await _read_value(drv, "meas.second") == pytest.approx(1500.5)
            # 两个 point_id 解析到同一 IOA，均可读。
            first = await drv.read([_ref("meas.first")])
            assert first[0].quality is Quality.GOOD
            # wire 到达的数据按 ioa→point_id 归属后者。
            assert all(pv.point_id == "meas.second" for pv in received)
        finally:
            await drv.close()

    async def test_reload_point_table(
        self, driver: IEC104Driver, server: IEC104MockServer
    ) -> None:
        """连接期间重载点表：旧 IOA 立即失效，新 IOA 总召后可读。"""
        await driver.connect()
        assert await _read_value(driver, "meas.power") == pytest.approx(1500.5)

        driver.set_points_mapping([_point("meas.only_freq", 200)])
        old = await driver.read([_ref("meas.power")])
        assert old[0].quality is Quality.BAD

        await driver.interrogate()
        assert await _read_value(driver, "meas.only_freq") == pytest.approx(50.0)


# ===========================================================================
# 生命周期清理
# ===========================================================================


class TestLifecycle:
    async def test_close_idempotent(self, driver: IEC104Driver) -> None:
        await driver.connect()
        await driver.close()
        await driver.close()
        assert driver.health().healthy is False

    async def test_close_without_connect(self) -> None:
        drv = IEC104Driver(_device_config(free_port()))
        await drv.close()
        assert drv.health().healthy is False
