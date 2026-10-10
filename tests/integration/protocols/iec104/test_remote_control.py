"""IEC104Driver 远程控制 × 真实 c104 从站集成测试。

对端是 :class:`tests.fixtures.servers.iec104_control_server.IEC104ControlServer`
（真实 TCP 从站，处理单点/双点/设点遥控命令：activation → ACT_CON →
ACT_TERM），覆盖三类控制命令的完整成功链路、写值校验与未寻址点错误，
以及总召数据经订阅分发。

与旧栈语义差异：控制命令类型由点表 ``ext.type_id`` 显式声明（可写点必填），
不再按 data_type/写值推断；未寻址 point_id 的写入抛 ConfigError。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

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
from tests.support.wait import wait_until

pytestmark = pytest.mark.real_service


def _command_point(point_id: str, ioa: int, type_id: str) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.WRITE,
        ext={"ioa": ioa, "type_id": type_id},
    )


def _monitoring_point(point_id: str, ioa: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ,
        ext={"ioa": ioa},
    )


def _driver(port: int, points: Sequence[Point]) -> IEC104Driver:
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        PointTable(
            "t1",
            Protocol("iec104"),
            {point.point_id: point for point in points},
        ),
        {"common_addr": 1, "t1": 2.0, "t2": 1.0, "t3": 5.0},
    )


@pytest.fixture
async def control_server() -> AsyncIterator[IEC104ControlServer]:
    server = IEC104ControlServer(common_addr=1, data_points={100: 1500.5})
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


class TestRemoteControlIntegration:
    """三类控制命令的完整成功链路（activation → ACT_CON → ACT_TERM）。"""

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
            result = await driver.write_one(
                ProtocolWrite(point_id="switch.on", value=True)
            )
        finally:
            await driver.close()

        assert result.point_id == "switch.on"
        assert result.success is True
        assert result.message is None
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
            result = await driver.write_one(
                ProtocolWrite(point_id="breaker.cmd", value=2)
            )
        finally:
            await driver.close()

        assert result.success is True
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
            result = await driver.write_one(
                ProtocolWrite(point_id="setpoint.val", value=42.5)
            )
        finally:
            await driver.close()

        assert result.success is True
        assert control_server.last_control.get("C_SE_NC_1") == 1003

    async def test_write_many_dispatches_all(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """write_many 逐点下发并保持请求顺序返回逐点结果。"""
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
            results = await driver.write_many(
                [
                    ProtocolWrite(point_id="switch.on", value=True),
                    ProtocolWrite(point_id="breaker.cmd", value=2),
                    ProtocolWrite(point_id="setpoint.val", value=42.5),
                ]
            )
        finally:
            await driver.close()

        assert [result.point_id for result in results] == [
            "switch.on",
            "breaker.cmd",
            "setpoint.val",
        ]
        assert all(result.success for result in results)

    async def test_invalid_value_fails_before_wire(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """C_SC_NA_1 只接受 bool：int 0 在编码期失败，success=False。"""
        driver = _driver(
            control_server.port,
            [_command_point("switch.off", 1001, "C_SC_NA_1")],
        )
        await driver.connect()
        try:
            result = await driver.write_one(
                ProtocolWrite(point_id="switch.off", value=0)
            )
        finally:
            await driver.close()

        assert result.success is False
        assert "boolean" in (result.message or "")
        # 值校验失败，命令不得上到 wire。
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
                await driver.write_one(
                    ProtocolWrite(point_id="no.such.point", value=True)
                )
        finally:
            await driver.close()

    async def test_write_raises_when_not_connected(self) -> None:
        """write_one 在未建连 driver 上立即抛 ProtocolError。"""
        driver = _driver(2404, [_command_point("p1", 1001, "C_SC_NA_1")])
        with pytest.raises(ProtocolError, match="OPEN connection"):
            await driver.write_one(ProtocolWrite(point_id="p1", value=True))


class TestInterrogationDispatch:
    """总召数据经订阅分发的端到端链路。"""

    async def test_subscribe_receives_interrogation_data(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """connect + 显式总召后，全局订阅者收到数据点样本。"""
        driver = _driver(
            control_server.port,
            [_monitoring_point("rotor.speed", 100)],
        )
        received: list[ProtocolSample] = []

        async def cb(sample: ProtocolSample) -> None:
            received.append(sample)

        await driver.subscribe([], cb)
        await driver.connect()
        try:
            await driver.interrogate()
            await wait_until(
                lambda: received or None,
                timeout=10.0,
                description="subscription receives interrogation data",
            )
        finally:
            await driver.close()

        speeds = [s for s in received if s.point_id == "rotor.speed"]
        assert speeds
        assert speeds[0].value == pytest.approx(1500.5)
        assert speeds[0].quality is Quality.GOOD
