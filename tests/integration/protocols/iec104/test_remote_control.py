"""IEC104Driver 远程控制 × 真实 c104 从站集成测试。

对端是 :class:`tests.fixtures.servers.iec104_control_server.IEC104ControlServer`
（真实 TCP 从站，处理单点/双点/设点遥控命令：activation → ACT_CON →
ACT_TERM），覆盖三类控制命令的确认链路、三分类结果、写值本地校验与
未寻址点错误，以及总召数据经本地 listen 分发。

命令类型由点表 ``ext.type_id`` 显式声明（可写点必填），不按写值推断；
未寻址 point_id 的写入抛 ConfigError。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from core.application import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    InterrogationRequest,
    ProtocolSample,
    ProtocolWrite,
    Quality,
    WriteOutcome,
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
from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.support.units import NONE_UNIT
from tests.support.wait import wait_until

pytestmark = pytest.mark.real_service


def _command_point(
    point_id: str,
    ioa: int,
    type_id: str,
    *,
    command_mode: str = "direct",
    confirmation: str = "act_term",
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.WRITE,
        ext={
            "ioa": ioa,
            "ca": 1,
            "type_id": type_id,
            "command_mode": command_mode,
            "confirmation": confirmation,
        },
    )


def _monitoring_point(point_id: str, ioa: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ,
        ext={"ioa": ioa, "ca": 1},
    )


def _driver(
    port: int,
    points: Sequence[Point],
    *,
    t1: float = 3.0,
) -> IEC104Driver:
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        PointTable(
            PointTableId("t1"),
            Protocol("iec104"),
            {point.point_id: point for point in points},
        ),
        {"common_addresses": "1", "t0": 5.0, "t1": t1, "t2": 1.0, "t3": 5.0},
    )


@pytest.fixture
async def control_server() -> AsyncIterator[IEC104ControlServer]:
    server = IEC104ControlServer(common_addr=1, data_points={100: 1500.5})
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def sbo_server() -> AsyncIterator[IEC104ControlServer]:
    # IOA 1001 在从站侧注册为 SELECT_AND_EXECUTE 模式。
    server = IEC104ControlServer(common_addr=1, data_points={100: 1500.5}, sbo_ioas={1001})
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def rejecting_server() -> AsyncIterator[IEC104ControlServer]:
    server = IEC104ControlServer(common_addr=1, data_points={100: 1500.5}, reject_ioas={1001})
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


class TestRemoteControlIntegration:
    """三类控制命令的确认链路（activation → 肯定 ACT_CON）。"""

    async def test_single_point_control_bool(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """C_SC_NA_1 bool=True → 肯定确认，从站记录命令到达。"""
        driver = _driver(
            control_server.port,
            [_command_point("switch.on", 1001, "C_SC_NA_1")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="switch.on", value=True))
        finally:
            await driver.close()

        assert result.point_id == "switch.on"
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert control_server.last_control.get("C_SC_NA_1") == 1001

    async def test_double_point_control(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """C_DC_NA_1 int 2 (ON) → 肯定确认。"""
        driver = _driver(
            control_server.port,
            [_command_point("breaker.cmd", 1002, "C_DC_NA_1")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="breaker.cmd", value=2))
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert control_server.last_control.get("C_DC_NA_1") == 1002

    async def test_set_point_control(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """C_SE_NC_1 float 42.5 → 肯定确认。"""
        driver = _driver(
            control_server.port,
            [_command_point("setpoint.val", 1003, "C_SE_NC_1")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="setpoint.val", value=42.5))
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert control_server.last_control.get("C_SE_NC_1") == 1003

    async def test_rejected_command_is_confirmed_failure(
        self,
        rejecting_server: IEC104ControlServer,
    ) -> None:
        """从站 on_receive 返回 FAILURE：结果必须是明确的 CONFIRMED_FAILURE。

        c104/lib60870 对 FAILURE 的线上行为（否定 ACT_CON 或静默）由本
        测试真实观测；无论帧形态如何，只要在 t1 窗口内拿到否定证据，就不
        允许归类为成功。若对端静默则按无确认证据归 UNKNOWN（见超时测试）。
        """
        driver = _driver(
            rejecting_server.port,
            [_command_point("switch.on", 1001, "C_SC_NA_1")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="switch.on", value=True))
        finally:
            await driver.close()

        # 实证（hat 主站 × c104/lib60870 从站）：on_receive FAILURE 对应
        # 线上否定 ACT_CON，三分类必须给出确定的 CONFIRMED_FAILURE。
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert rejecting_server.last_control.get("C_SC_NA_1") == 1001

    async def test_write_many_is_not_supported(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """IEC104 不支持批量写：write_many 必须显式拒绝，不得逐点下发。"""
        driver = _driver(
            control_server.port,
            [
                _command_point("switch.on", 1001, "C_SC_NA_1"),
                _command_point("breaker.cmd", 1002, "C_DC_NA_1"),
                _command_point("setpoint.val", 1003, "C_SE_NC_1"),
            ],
        )
        await driver.connect()
        try:
            with pytest.raises(ProtocolCapabilityError, match="batch write"):
                await driver.write_many(
                    (
                        ProtocolWrite(point="switch.on", value=True),
                        ProtocolWrite(point="breaker.cmd", value=2),
                        ProtocolWrite(point="setpoint.val", value=42.5),
                    )
                )
        finally:
            await driver.close()

        assert control_server.last_control == {}

    async def test_invalid_value_fails_before_wire(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """C_SC_NA_1 只接受 bool：int 0 在编码期失败（CONFIRMED_FAILURE，未上 wire）。"""
        driver = _driver(
            control_server.port,
            [_command_point("switch.off", 1001, "C_SC_NA_1")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="switch.off", value=0))
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert "boolean" in (result.message or "")
        assert "C_SC_NA_1" not in control_server.last_control

    async def test_unknown_point_raises(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """写入未寻址 point_id 属于配置错误，抛 ConfigError。"""
        driver = _driver(control_server.port, [])
        await driver.connect()
        try:
            with pytest.raises(ConfigError, match="no.such.point"):
                await driver.write_one(ProtocolWrite(point="no.such.point", value=True))
        finally:
            await driver.close()

    async def test_write_raises_when_not_connected(self) -> None:
        """write_one 在未建连 driver 上立即抛 ProtocolError，不假装排队。"""
        from tests.support.process import free_port

        driver = _driver(free_port(), [_command_point("p1", 1001, "C_SC_NA_1")])
        with pytest.raises(ProtocolError, match="session unavailable"):
            await driver.write_one(ProtocolWrite(point="p1", value=True))
        await driver.close()

    async def test_unknown_command_ioa_is_confirmed_failure(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """从站未注册的控制 IOA：c104/lib60870 实证回否定 ACT_CON。"""
        driver = _driver(
            control_server.port,
            [_command_point("ghost.cmd", 4999, "C_SC_NA_1")],
            t1=3.0,
        )
        await driver.connect()
        try:
            result = await asyncio.wait_for(
                driver.write_one(ProtocolWrite(point="ghost.cmd", value=True)),
                timeout=10.0,
            )
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE

    async def test_sbo_select_rejected_by_direct_mode_slave_is_confirmed_failure(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """主站按 SBO 发 SELECT，而从站点位是直控模式：c104 内部拒绝选择。

        结果必须是确定的 CONFIRMED_FAILURE（SELECT rejected），且选择阶段
        被拒后不得再发 EXECUTE——从站 on_receive 不应被调用。
        """
        driver = _driver(
            control_server.port,
            [_command_point("switch.on", 1001, "C_SC_NA_1", command_mode="select_execute")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="switch.on", value=True))
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert "SELECT rejected" in (result.message or "")
        assert control_server.last_control == {}

    async def test_sbo_select_execute_full_chain_succeeds(
        self,
        sbo_server: IEC104ControlServer,
    ) -> None:
        """SBO 从站：SELECT 肯定 ACT_CON → EXECUTE ACT_CON → ACT_TERM 全链成功。"""
        driver = _driver(
            sbo_server.port,
            [_command_point("switch.on", 1001, "C_SC_NA_1", command_mode="select_execute")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(ProtocolWrite(point="switch.on", value=True))
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS, result.message
        assert sbo_server.last_control.get("C_SC_NA_1") == 1001

    async def test_act_con_confirmation_completes_without_act_term(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """confirmation=act_conf：拿到肯定 ACT_CON 即成功，不等待 ACT_TERM。

        从站会在 50ms 后补发 ACT_TERM，但 driver 分类不依赖它；
        ACT_CON 窗口 t1×2 内完成即 CONFIRMED_SUCCESS。
        """
        driver = _driver(
            control_server.port,
            [_command_point("switch.fast", 1001, "C_SC_NA_1", confirmation="act_con")],
        )
        await driver.connect()
        try:
            result = await asyncio.wait_for(
                driver.write_one(ProtocolWrite(point="switch.fast", value=True)),
                timeout=5.0,
            )
        finally:
            await driver.close()

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS, result.message

    async def test_command_timeout_with_transport_acks_only_is_unknown(self) -> None:
        """黑洞只回 S 帧传输确认、无 ACT_CON：t1×2 后归 UNKNOWN（可能已执行）。

        传输 ACK 不等于应用确认——这是写分类的核心边界。
        """
        from tests.support.iec104_blackhole import StartdtBlackhole
        from tests.support.process import free_port

        blackhole = StartdtBlackhole(free_port())
        await blackhole.start()
        driver = _driver(
            blackhole.port,
            [_command_point("switch.on", 1001, "C_SC_NA_1")],
            t1=1.0,
        )
        try:
            await driver.connect()
            result = await asyncio.wait_for(
                driver.write_one(ProtocolWrite(point="switch.on", value=True)),
                timeout=10.0,
            )
        finally:
            await driver.close()
            await blackhole.stop()

        assert result.outcome is WriteOutcome.UNKNOWN
        assert "may have executed" in (result.message or "")


class TestInterrogationDispatch:
    """总召数据经本地 listen 分发的端到端链路。"""

    async def test_listen_receives_interrogation_data(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """connect + listen + 显式总召后，监听者收到数据点样本。"""
        driver = _driver(
            control_server.port,
            [_monitoring_point("rotor.speed", 100)],
        )
        received: list[ProtocolSample] = []

        async def cb(samples: tuple[ProtocolSample, ...]) -> None:
            for sample in samples:
                received.append(sample)

        await driver.connect()
        await driver.listen((), cb)
        try:
            await driver.interrogate(InterrogationRequest())
            await wait_until(
                lambda: received or None,
                timeout=10.0,
                description="listener receives interrogation data",
            )
        finally:
            await driver.close()

        speeds = [s for s in received if s.point_id == "rotor.speed"]
        assert speeds
        assert speeds[0].value == pytest.approx(1500.5, abs=1e-4)
        assert speeds[0].quality is Quality.GOOD
