"""Integration test — IEC104 remote control and spontaneous updates.

Uses :class:`tests.fixtures.servers.iec104_control_server.IEC104ControlServer`
(real TCP server handling single-point, double-point, and set-point remote
control commands: activation → ACT_CON → ACT_TERM). Also tests spontaneous
update dispatch via the driver.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from wind_hub_core.config import DeviceConfig, PointAddress, PointConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.point import PointValue, Quality
from wind_hub_core.protocol.iec104.driver import IEC104Driver

pytestmark = pytest.mark.real_service

# ===========================================================================
# helpers
# ===========================================================================


def _make_device_config(port: int) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-rtu",
        protocol="iec104",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=port,
            extensions={"common_addr": 1, "t1": 2.0, "t2": 1.0, "t3": 5.0},
        ),
        point_table="t1",
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
# fixture
# ===========================================================================


@pytest.fixture
async def control_server() -> Any:
    server = IEC104ControlServer(common_addr=1, data_points={100: 1500.5})
    await server.start()
    yield server
    await server.stop()


# ===========================================================================
# tests
# ===========================================================================


class TestRemoteControlIntegration:
    """End-to-end remote control against a real TCP server."""

    async def test_single_point_control_bool(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """Send bool=True → C_SC_NA_1 → complete success."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("switch.on", 1001, data_type="bool"),
            ]
        )

        await driver.connect()
        try:
            results = await driver.write(
                [
                    Command(
                        command_id="c1",
                        device_id="test-rtu",
                        point_id="switch.on",
                        value=True,
                    ),
                ]
            )
        finally:
            await driver.close()

        assert len(results) == 1
        assert results[0].command_id == "c1"
        assert results[0].success is True
        assert results[0].error is None

    async def test_single_point_control_int_zero(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """Send int 0 → C_SC_NA_1 → complete success."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("switch.off", 1001, data_type="uint16"),
            ]
        )

        await driver.connect()
        try:
            results = await driver.write(
                [
                    Command(
                        command_id="c2",
                        device_id="test-rtu",
                        point_id="switch.off",
                        value=0,
                    ),
                ]
            )
        finally:
            await driver.close()

        assert len(results) == 1
        assert results[0].success is True

    async def test_double_point_control(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """Send int 2 (ON) → C_DC_NA_1 → complete success."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("breaker.cmd", 1002, data_type="uint16"),
            ]
        )

        await driver.connect()
        try:
            results = await driver.write(
                [
                    Command(
                        command_id="c3",
                        device_id="test-rtu",
                        point_id="breaker.cmd",
                        value=2,  # ON
                    ),
                ]
            )
        finally:
            await driver.close()

        assert len(results) == 1
        assert results[0].success is True

    async def test_set_point_control(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """Send float 42.5 → C_SE_NC_1 → complete success."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("setpoint.val", 1003, data_type="float32"),
            ]
        )

        await driver.connect()
        try:
            results = await driver.write(
                [
                    Command(
                        command_id="c4",
                        device_id="test-rtu",
                        point_id="setpoint.val",
                        value=42.5,
                    ),
                ]
            )
        finally:
            await driver.close()

        assert len(results) == 1
        assert results[0].success is True

    async def test_unknown_point_fails(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """Write to an unmapped point returns failure."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        # No points mapped.

        await driver.connect()
        try:
            results = await driver.write(
                [
                    Command(
                        command_id="c5",
                        device_id="test-rtu",
                        point_id="no.such.point",
                        value=True,
                    ),
                ]
            )
        finally:
            await driver.close()

        assert len(results) == 1
        assert results[0].success is False
        assert "unknown point" in (results[0].error or "").lower()

    async def test_write_raises_when_not_connected(self) -> None:
        """write() on a disconnected driver raises ProtocolError."""
        from wind_hub_core.model.errors import ProtocolError

        cfg = _make_device_config(2404)
        driver = IEC104Driver(cfg)
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.write(
                [
                    Command(
                        command_id="c6",
                        device_id="test-rtu",
                        point_id="p1",
                        value=True,
                    ),
                ]
            )

class TestSpontaneousUpdateIntegration:
    """End-to-end spontaneous update via driver subscription."""

    async def test_subscribe_dispatches_on_startup(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """After connect + interrogation, global subscriber receives values."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("rotor.speed", 100, data_type="float32"),
            ]
        )

        received: list[PointValue] = []

        async def cb(pv: PointValue) -> None:
            received.append(pv)

        await driver.subscribe([], cb)

        await driver.connect()
        try:
            # Interrogation happens during connect → values dispatched.
            await asyncio.sleep(0.5)
        finally:
            await driver.close()

        # The interrogation should have dispatched the data point.
        assert len(received) >= 1
        speeds = [pv for pv in received if pv.point_id == "rotor.speed"]
        assert len(speeds) >= 1
        assert speeds[0].value == 1500.5
        assert speeds[0].quality == Quality.GOOD
        assert speeds[0].source == "iec104"
