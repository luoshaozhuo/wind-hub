"""IEC104 slave session — one TCP connection's frame loop.

Reads APDU frames from the master, answers the STARTDT/TESTFR/STOPDT
unnumbered handshake, and dispatches decoded I-frames to the shared
:class:`IEC104SlaveHandlers`.  Outbound ``send_asdu`` wraps an ASDU in an
I-frame and tracks this session's send sequence number (N(S)); the receive
sequence number (N(R)) piggy-backs an acknowledgement of the master's last
I-frame.

Flow control is minimal but interoperable: the slave acknowledges the master
in the N(R) of its own I-frames and answers U-frames directly, matching the
behaviour our own IEC104 *master* driver expects of a slave.
"""

from __future__ import annotations

import asyncio
import logging

from wind_hub.adapter.inbound.iec104_slave.handlers import IEC104SlaveHandlers
from wind_hub.adapter.outbound.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    IFrame,
    SFrame,
    TypeID,
    UFrame,
    UFrameType,
    decode_apdu,
    decode_asdu,
    encode_asdu,
    encode_i_frame,
    encode_u_frame,
)

logger = logging.getLogger(__name__)

START_CHAR = 0x68
MAX_SEQ = 0x7FFF

# Control-direction TypeIDs that map to remote-control commands.
_COMMAND_TYPE_IDS: frozenset[TypeID] = frozenset(
    {TypeID.C_SC_NA_1, TypeID.C_DC_NA_1, TypeID.C_SE_NC_1}
)


class IEC104SlaveSession:
    """A single IEC104 slave TCP session, bound to one ``(reader, writer)``."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handlers: IEC104SlaveHandlers,
        common_address: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handlers = handlers
        self._common_address = common_address

        self._send_seq = 0
        self._recv_seq = 0
        self._started = False

    async def run(self) -> None:
        """Run the session until the peer disconnects or the socket errors."""
        while True:
            frame_bytes = await self._read_frame()
            if frame_bytes is None:
                return

            frame = decode_apdu(frame_bytes)
            if isinstance(frame, UFrame):
                await self._handle_u_frame(frame)
            elif isinstance(frame, SFrame):
                # Supervisory ack — nothing to do beyond noting the peer's N(R).
                self._recv_seq = (frame.recv_seq + 1) & MAX_SEQ
            elif isinstance(frame, IFrame):
                self._recv_seq = (frame.send_seq + 1) & MAX_SEQ
                if not self._started:
                    continue
                asdu, _ = decode_asdu(frame.asdu)
                await self._dispatch(asdu)

    async def send_asdu(self, asdu: ASDU, negative: bool = False) -> None:
        """Encode and send *asdu* in an I-frame.

        A ``negative=True`` confirmation sets the cause-of-transmission
        P/N bit (bit 7) — the IEC104 "negative acknowledgement" marker.
        """
        raw = encode_asdu(asdu)
        if negative:
            raw = raw[:2] + bytes([raw[2] | 0x80]) + raw[3:]
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) & MAX_SEQ
        await self._write(encode_i_frame(seq, self._recv_seq, raw))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _read_frame(self) -> bytes | None:
        """Read one complete APDU frame, or ``None`` on EOF / bad start byte."""
        try:
            header = await self._reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        if header[0] != START_CHAR:
            return None
        try:
            rest = await self._reader.readexactly(header[1])
        except asyncio.IncompleteReadError:
            return None
        return header + rest

    async def _handle_u_frame(self, frame: UFrame) -> None:
        if frame.frame_type == UFrameType.STARTDT_ACT:
            self._started = True
            logger.debug(
                "IEC104 slave: STARTDT (common_address=%d) from peer", self._common_address
            )
            await self._write(encode_u_frame(UFrameType.STARTDT_CON))
        elif frame.frame_type == UFrameType.STOPDT_ACT:
            self._started = False
            await self._write(encode_u_frame(UFrameType.STOPDT_CON))
        elif frame.frame_type == UFrameType.TESTFR_ACT:
            await self._write(encode_u_frame(UFrameType.TESTFR_CON))
        # STARTDT_CON / STOPDT_CON / TESTFR_CON originate from a slave and
        # are unexpected here — ignored.

    async def _dispatch(self, asdu: ASDU) -> None:
        if asdu.type_id == TypeID.C_IC_NA_1 and asdu.cause == CauseOfTransmission.ACTIVATION:
            await self._handlers.handle_interrogation(asdu, self)
        elif asdu.type_id in _COMMAND_TYPE_IDS and asdu.cause == CauseOfTransmission.ACTIVATION:
            await self._handlers.handle_command(asdu, self)

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()
