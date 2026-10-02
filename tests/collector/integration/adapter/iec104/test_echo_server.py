"""Integration test — IEC104 echo server.

Starts a simple IEC104 TCP server that handles STARTDT, station
interrogation, and TESTFR.  A real session connects, completes the
handshake + interrogation, and verifies the point cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import Any

import pytest

from wind_hub.adapter.outbound.protocol.iec104.session import IEC104Session
from wind_hub_core.model.errors import ProtocolError

logger = logging.getLogger(__name__)

START_CHAR = 0x68

# Pre-built frames for the echo server.
# These are raw APDU bytes encoding known values.

# STARTDT_CON U-frame: 0x68 | 0x04 (len) | 0x0B 0x00 0x00 0x00
_STARTDT_CON = bytes([0x68, 0x04, 0x0B, 0x00, 0x00, 0x00])

# TESTFR_CON U-frame: 0x68 | 0x04 | 0x83 0x00 0x00 0x00
_TESTFR_CON = bytes([0x68, 0x04, 0x83, 0x00, 0x00, 0x00])

# STOPDT_CON U-frame: 0x68 | 0x04 | 0x23 0x00 0x00 0x00
_STOPDT_CON = bytes([0x68, 0x04, 0x23, 0x00, 0x00, 0x00])


def _build_apdu(body: bytes) -> bytes:
    """Build a complete APDU: 0x68 | len | body."""
    return bytes([START_CHAR, len(body)]) + body


def _encode_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    """Encode an I-frame.

    Control field: bytes 0-1 = (send_seq << 1), bytes 2-3 = (recv_seq << 1).
    """
    ctrl = struct.pack(
        "<HH",
        (send_seq << 1) & 0xFFFF,
        (recv_seq << 1) & 0xFFFF,
    )
    return _build_apdu(ctrl + asdu)


def _make_m_me_nc_1_asdu(ioa: int, value: float, common_addr: int) -> bytes:
    """Build an M_ME_NC_1 ASDU with a single info object.

    ASDU header (6 bytes): TypeID(1) | VSQ(1) | COT(1) | OA(1) | CA(2)
    Info object (8 bytes): IOA(3) | float32(4) | QDS(1)

    TypeID=13 (M_ME_NC_1), COT=20 (INTERROGATED_BY_STATION).
    """
    header = bytearray()
    header.append(0x0D)  # TypeID = M_ME_NC_1
    header.append(0x01)  # VSQ = count=1, SQ=0
    header.append(0x14)  # COT = INTERROGATED_BY_STATION (20)
    header.append(0x00)  # OA = 0
    header.extend(struct.pack("<H", common_addr))  # CA

    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])  # IOA (3 bytes LE)
    body.extend(struct.pack("<f", value))  # float32
    body.append(0x00)  # QDS = good

    return bytes(header) + bytes(body)


def _make_c_ic_na_1_asdu(
    cause: int,
    ioa: int,
    common_addr: int,
) -> bytes:
    """Build a C_IC_NA_1 ASDU.

    TypeID=100 (C_IC_NA_1), count=1.
    """
    header = bytearray()
    header.append(0x64)  # TypeID = C_IC_NA_1
    header.append(0x01)  # VSQ = count=1, SQ=0
    header.append(cause & 0x3F)
    header.append(0x00)  # OA = 0
    header.extend(struct.pack("<H", common_addr))  # CA

    # Info object: IOA(3) + QOI(1) = 4 bytes.
    body = bytearray()
    body.extend(struct.pack("<I", ioa)[:3])  # IOA
    body.append(0x14)  # QOI = 20 (station)

    return bytes(header) + bytes(body)


# ===========================================================================
# echo server
# ===========================================================================


class IEC104EchoServer:
    """A minimal IEC104 slave that handles:

    - STARTDT_ACT → STARTDT_CON
    - C_IC_NA_1 (QOI=20) → ACT_CON + data + ACT_TERM
    - TESTFR_ACT → TESTFR_CON
    - STOPDT_ACT → STOPDT_CON
    """

    def __init__(
        self,
        common_addr: int = 1,
        data_points: dict[int, float] | None = None,
    ) -> None:
        self._common_addr = common_addr
        self._data_points = data_points or {
            100: 1500.5,
            200: 50.0,
            300: 0.75,
        }
        self._server: asyncio.AbstractServer | None = None
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """Start listening on a random port."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
        )
        addr = self._server.sockets[0].getsockname()  # type: ignore[index]
        self._port = addr[1]
        logger.info("IEC104 echo server listening on port %d", self._port)

    async def stop(self) -> None:
        """Stop the server."""
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
        """Handle one TCP connection."""
        send_seq = 0
        recv_seq = 0
        logger.info("Echo server: client connected")

        try:
            while True:
                # Read APDU frame.
                frame = await self._read_frame(reader)
                if frame is None:
                    break

                # Parse control field.
                len(frame) - 2
                ctrl = frame[2:6]
                asdu_data = frame[6:]

                if (ctrl[0] & 0x01) == 0:
                    # I-frame.
                    peer_send = (struct.unpack_from("<H", ctrl, 0)[0] >> 1) & 0x7FFF
                    (struct.unpack_from("<H", ctrl, 2)[0] >> 1) & 0x7FFF
                    recv_seq = peer_send + 1 & 0x7FFF
                    await self._handle_i_frame(
                        writer,
                        asdu_data,
                        send_seq,
                        recv_seq,
                    )
                    send_seq = (send_seq + 1) & 0x7FFF

                elif (ctrl[0] & 0x03) == 0x01:
                    # S-frame.
                    pass

                elif (ctrl[0] & 0x03) == 0x03:
                    # U-frame.
                    func_code = ctrl[0]
                    await self._handle_u_frame(writer, func_code)

        except Exception:
            logger.exception("Echo server: error handling client")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_frame(
        self,
        reader: asyncio.StreamReader,
    ) -> bytearray | None:
        """Read one complete APDU frame."""
        try:
            data = await reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None

        if data[0] != START_CHAR:
            return None

        apdu_len = data[1]
        2 + apdu_len
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
        """Handle a U-frame."""
        if func_code == 0x07:  # STARTDT_ACT
            writer.write(_STARTDT_CON)
            await writer.drain()
            logger.info("Echo server: STARTDT_ACT → STARTDT_CON")

        elif func_code == 0x13:  # STOPDT_ACT
            writer.write(_STOPDT_CON)
            await writer.drain()
            logger.info("Echo server: STOPDT_ACT → STOPDT_CON")

        elif func_code == 0x43:  # TESTFR_ACT
            writer.write(_TESTFR_CON)
            await writer.drain()
            logger.info("Echo server: TESTFR_ACT → TESTFR_CON")

    async def _handle_i_frame(
        self,
        writer: asyncio.StreamWriter,
        asdu_data: bytes,
        send_seq: int,
        recv_seq: int,
    ) -> None:
        """Handle an I-frame — check for interrogation command."""
        if len(asdu_data) < 6:
            return

        type_id = asdu_data[0]
        cause = asdu_data[2] & 0x3F
        # NOTE: VSQ in byte 1 — count in low 7 bits.

        if type_id == 0x64 and cause == 0x06:  # C_IC_NA_1, ACTIVATION
            logger.info("Echo server: received interrogation activation")
            await self._send_interrogation_response(writer, send_seq, recv_seq)

    async def _send_interrogation_response(
        self,
        writer: asyncio.StreamWriter,
        send_seq: int,
        recv_seq: int,
    ) -> None:
        """Send the full interrogation response: ACT_CON + data + ACT_TERM."""
        # 1. ACTIVATION_CON
        act_con_asdu = _make_c_ic_na_1_asdu(
            cause=0x07,  # ACTIVATION_CON
            ioa=0,
            common_addr=self._common_addr,
        )
        frame = _encode_i_frame(send_seq, recv_seq, act_con_asdu)
        writer.write(frame)
        await writer.drain()
        send_seq = (send_seq + 1) & 0x7FFF

        # 2. Data I-frames — one per data point.
        for ioa, value in self._data_points.items():
            meas_asdu = _make_m_me_nc_1_asdu(
                ioa=ioa,
                value=value,
                common_addr=self._common_addr,
            )
            frame = _encode_i_frame(send_seq, recv_seq, meas_asdu)
            writer.write(frame)
            await writer.drain()
            send_seq = (send_seq + 1) & 0x7FFF

        # 3. ACTIVATION_TERMINATION
        act_term_asdu = _make_c_ic_na_1_asdu(
            cause=0x0A,  # ACTIVATION_TERMINATION
            ioa=0,
            common_addr=self._common_addr,
        )
        frame = _encode_i_frame(send_seq, recv_seq, act_term_asdu)
        writer.write(frame)
        await writer.drain()

        logger.info("Echo server: interrogation response sent (3 data points)")


# ===========================================================================
# tests
# ===========================================================================


@pytest.fixture
async def echo_server() -> Any:
    """Start an echo server and yield it."""
    server = IEC104EchoServer(
        common_addr=1,
        data_points={100: 1500.5, 200: 50.0, 300: 0.75},
    )
    await server.start()
    yield server
    await server.stop()


class TestSessionWithEchoServer:
    """Integration test — real TCP session against the echo server."""

    async def test_full_handshake_and_interrogation(
        self,
        echo_server: IEC104EchoServer,
    ) -> None:
        """Connect, STARTDT, interrogate, verify cache."""
        session = IEC104Session(
            host="127.0.0.1",
            port=echo_server.port,
            common_addr=1,
            k=12,
            w=8,
            t1=2.0,
            t2=1.0,
            t3=5.0,
        )
        session.set_points_mapping(
            {100: "rotor.speed", 200: "gen.power", 300: "pitch.angle"},
            {"rotor.speed": 100, "gen.power": 200, "pitch.angle": 300},
        )

        try:
            await asyncio.wait_for(session.start(), timeout=10.0)
        finally:
            await session.close()

        # Verify interrogation completed.
        assert session.interrogation_complete

        # Verify point cache.
        cache = session.point_cache
        assert 100 in cache
        assert 200 in cache
        assert 300 in cache

        assert cache[100].point_id == "rotor.speed"
        assert cache[100].value == 1500.5
        assert cache[200].point_id == "gen.power"
        assert cache[200].value == 50.0
        assert cache[300].point_id == "pitch.angle"
        assert cache[300].value == 0.75

    async def test_connection_refused(self) -> None:
        """Connecting to a closed port should raise ProtocolError."""
        session = IEC104Session(
            host="127.0.0.1",
            port=19999,  # Unlikely to be open.
            common_addr=1,
            t1=0.5,
        )
        with pytest.raises(ProtocolError, match="TCP connect"):
            await session.start()
        await session.close()

    async def test_startdt_timeout(self, echo_server: IEC104EchoServer) -> None:
        """If the server never sends STARTDT_CON, we time out."""
        # We need a server that accepts but never responds.
        # Reuse echo server port — but close it first and replace
        # with a silent server.

        # Create a temporary server that eats all data silently.
        async def _silent_handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                await reader.read(1024)  # Read once and hang.
                # Never respond.
                await asyncio.sleep(5)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

        silent_srv = await asyncio.start_server(
            _silent_handler,
            host="127.0.0.1",
            port=0,
        )
        silent_port = silent_srv.sockets[0].getsockname()[1]  # type: ignore[index]

        try:
            session = IEC104Session(
                host="127.0.0.1",
                port=silent_port,
                common_addr=1,
                t1=0.3,
            )
            with pytest.raises(ProtocolError, match="STARTDT handshake timed out"):
                await session.start()
            await session.close()
        finally:
            silent_srv.close()
            with contextlib.suppress(Exception):
                await silent_srv.wait_closed()
