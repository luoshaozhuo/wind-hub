"""Integration test — IEC104 remote control and spontaneous updates.

Extends the echo server to handle single-point, double-point, and
set-point remote control commands (activation → ACT_CON → ACT_TERM).
Also tests spontaneous update dispatch via the driver.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import Any

import pytest

from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.point import PointValue, Quality

logger = logging.getLogger(__name__)

START_CHAR = 0x68

# --- pre-built U-frames ---

_STARTDT_CON = bytes([0x68, 0x04, 0x0B, 0x00, 0x00, 0x00])
_TESTFR_CON = bytes([0x68, 0x04, 0x83, 0x00, 0x00, 0x00])
_STOPDT_CON = bytes([0x68, 0x04, 0x23, 0x00, 0x00, 0x00])


def _build_apdu(body: bytes) -> bytes:
    return bytes([START_CHAR, len(body)]) + body


def _encode_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    ctrl = struct.pack(
        "<HH",
        (send_seq << 1) & 0xFFFF,
        (recv_seq << 1) & 0xFFFF,
    )
    return _build_apdu(ctrl + asdu)


# ===========================================================================
# ASDU builders
# ===========================================================================


def _make_c_ic_na_1_asdu(cause: int, ioa: int, common_addr: int) -> bytes:
    header = bytearray()
    header.append(0x64)  # TypeID = C_IC_NA_1
    header.append(0x01)  # VSQ = count=1
    header.append(cause & 0x3F)
    header.append(0x00)  # OA
    header.extend(struct.pack("<H", common_addr))
    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])
    body.append(0x14)  # QOI = 20 (station)
    return bytes(header) + bytes(body)


def _make_m_me_nc_1_asdu(ioa: int, value: float, common_addr: int) -> bytes:
    header = bytearray()
    header.append(0x0D)  # TypeID = M_ME_NC_1
    header.append(0x01)  # VSQ = count=1
    header.append(0x14)  # COT = INTERROGATED_BY_STATION
    header.append(0x00)  # OA
    header.extend(struct.pack("<H", common_addr))
    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])
    body.extend(struct.pack("<f", value))
    body.append(0x00)  # QDS = good
    return bytes(header) + bytes(body)


def _make_control_con_asdu(
    type_id: int,
    cause: int,
    ioa: int,
    common_addr: int,
) -> bytes:
    """Build an activation confirmation or termination ASDU.

    For single-point control (C_SC_NA_1, type_id=45),
    the info object is: IOA(3) + SCO(1) = 4 bytes.
    SCO = 0x00 (off).
    """
    header = bytearray()
    header.append(type_id)
    header.append(0x01)  # VSQ = count=1
    header.append(cause & 0x3F)
    header.append(0x00)  # OA
    header.extend(struct.pack("<H", common_addr))
    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])
    body.append(0x00)  # SCO = off (doesn't matter for con)
    return bytes(header) + bytes(body)


# ===========================================================================
# remote-control echo server
# ===========================================================================


class IEC104ControlServer:
    """Echo server extended with remote-control support.

    Handles:
    - C_SC_NA_1 (single-point): ACT→ACT_CON→ACT_TERM
    - C_DC_NA_1 (double-point): ACT→ACT_CON→ACT_TERM
    - C_SE_NC_1 (set-point): ACT→ACT_CON→ACT_TERM
    - Station interrogation (C_IC_NA_1)
    - Spontaneous update on demand
    """

    _CONTROL_TYPE_IDS = {
        45: 0x04,  # C_SC_NA_1: info obj size = 4
        46: 0x04,  # C_DC_NA_1: info obj size = 4
        50: 0x05,  # C_SE_NC_1: info obj size = 5
    }

    def __init__(
        self,
        common_addr: int = 1,
        data_points: dict[int, float] | None = None,
    ) -> None:
        self._common_addr = common_addr
        self._data_points = data_points or {100: 1500.5}
        self._server: asyncio.AbstractServer | None = None
        self._port: int = 0
        self._last_control: dict[str, Any] = {}  # TypeID → value

    @property
    def port(self) -> int:
        return self._port

    @property
    def last_control(self) -> dict[str, Any]:
        return dict(self._last_control)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
        )
        addr = self._server.sockets[0].getsockname()  # type: ignore[index]
        self._port = addr[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        send_seq = 0
        recv_seq = 0

        try:
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    break

                ctrl = frame[2:6]
                asdu_data = frame[6:]

                if (ctrl[0] & 0x01) == 0:  # I-frame
                    peer_send = (struct.unpack_from("<H", ctrl, 0)[0] >> 1) & 0x7FFF
                    recv_seq = (peer_send + 1) & 0x7FFF
                    await self._handle_i_frame(
                        writer,
                        asdu_data,
                        send_seq,
                        recv_seq,
                    )
                    send_seq = (send_seq + 1) & 0x7FFF
                elif (ctrl[0] & 0x03) == 0x03:  # U-frame
                    await self._handle_u_frame(writer, ctrl[0])

        except Exception:
            logger.exception("Control server error")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_frame(
        self,
        reader: asyncio.StreamReader,
    ) -> bytearray | None:
        try:
            data = await reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        if data[0] != START_CHAR:
            return None
        apdu_len = data[1]
        try:
            rest = await reader.readexactly(apdu_len)
        except asyncio.IncompleteReadError:
            return None
        return bytearray(data) + bytearray(rest)

    async def _handle_u_frame(
        self,
        writer: asyncio.StreamWriter,
        func_code: int,
    ) -> None:
        if func_code == 0x07:
            writer.write(_STARTDT_CON)
            await writer.drain()
        elif func_code == 0x13:
            writer.write(_STOPDT_CON)
            await writer.drain()
        elif func_code == 0x43:
            writer.write(_TESTFR_CON)
            await writer.drain()

    async def _handle_i_frame(
        self,
        writer: asyncio.StreamWriter,
        asdu_data: bytes,
        send_seq: int,
        recv_seq: int,
    ) -> None:
        if len(asdu_data) < 6:
            return
        type_id = asdu_data[0]
        cause = asdu_data[2] & 0x3F

        if type_id == 0x64 and cause == 0x06:  # C_IC_NA_1, ACTIVATION
            await self._send_interrogation_response(writer, send_seq, recv_seq)
        elif type_id in self._CONTROL_TYPE_IDS and cause == 0x06:  # Control ACT
            ioa = int.from_bytes(asdu_data[6:9], "little")
            await self._send_control_response(
                writer,
                type_id,
                ioa,
                send_seq,
                recv_seq,
            )

    async def _send_interrogation_response(
        self,
        writer: asyncio.StreamWriter,
        send_seq: int,
        recv_seq: int,
    ) -> None:
        # ACT_CON
        frame = _encode_i_frame(
            send_seq,
            recv_seq,
            _make_c_ic_na_1_asdu(0x07, 0, self._common_addr),
        )
        writer.write(frame)
        await writer.drain()
        s = (send_seq + 1) & 0x7FFF

        # Data points
        for ioa, value in self._data_points.items():
            frame = _encode_i_frame(
                s,
                recv_seq,
                _make_m_me_nc_1_asdu(ioa, value, self._common_addr),
            )
            writer.write(frame)
            await writer.drain()
            s = (s + 1) & 0x7FFF

        # ACT_TERM
        frame = _encode_i_frame(
            s,
            recv_seq,
            _make_c_ic_na_1_asdu(0x0A, 0, self._common_addr),
        )
        writer.write(frame)
        await writer.drain()

    async def _send_control_response(
        self,
        writer: asyncio.StreamWriter,
        type_id: int,
        ioa: int,
        send_seq: int,
        recv_seq: int,
    ) -> None:
        s = send_seq

        # Record the control.
        self._last_control[str(type_id)] = ioa

        # 1. ACT_CON (COT=7)
        frame = _encode_i_frame(
            s,
            recv_seq,
            _make_control_con_asdu(type_id, 0x07, ioa, self._common_addr),
        )
        writer.write(frame)
        await writer.drain()
        s = (s + 1) & 0x7FFF

        # Small delay to simulate real device processing.
        await asyncio.sleep(0.05)

        # 2. ACT_TERM (COT=10)
        frame = _encode_i_frame(
            s,
            recv_seq,
            _make_control_con_asdu(type_id, 0x0A, ioa, self._common_addr),
        )
        writer.write(frame)
        await writer.drain()


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
                _make_point_config("switch.on", 100, data_type="bool"),
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
                _make_point_config("switch.off", 100, data_type="uint16"),
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
                _make_point_config("breaker.cmd", 200, data_type="uint16"),
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
                _make_point_config("setpoint.val", 300, data_type="float32"),
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
        from wind_hub.domain.model.errors import ProtocolError

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

    async def test_close_with_pending_send_queue(
        self,
        control_server: IEC104ControlServer,
    ) -> None:
        """close() completes even when the session's send queue still holds
        undrained frames (regression for the step 7b-3 close() reorder)."""
        cfg = _make_device_config(control_server.port)
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("switch.on", 100, data_type="bool"),
            ]
        )

        await driver.connect()
        assert driver._session is not None

        # Queue an undrained frame so close() must tear down a send task
        # that still has work pending (send queue non-empty at close time).
        driver._session._enqueue_frame_nowait(b"\x68\x04\x43\x00\x00\x00")

        # Must return promptly instead of hanging on the send task.
        await asyncio.wait_for(driver.close(), timeout=5.0)


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
