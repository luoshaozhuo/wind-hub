"""IEC 60870-5-104 protocol session.

Orchestrates a single TCP connection to an IEC104 slave: handshake,
station interrogation, keep-alive, and data forwarding.

Architecture
------------

The session spawns two background tasks once the TCP connection is
established:

* ``_receive_loop`` — reads raw bytes from the socket, parses APDU
  frames, and dispatches I/S/U frames to the appropriate handler.
* ``_send_loop`` — drains an ``asyncio.Queue`` of outbound APDU bytes
  and writes them to the socket.

Timer callbacks (t1/t2/t3) are installed on :class:`IEC104Timers` and
fired asynchronously by background timer tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from wind_hub.adapter.outbound.protocol.iec104.codec.apci import (
    IFrame,
    SFrame,
    UFrame,
    decode_apdu,
    encode_i_frame,
    encode_s_frame,
    encode_u_frame,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.asdu import (
    ASDU,
    decode_asdu,
    encode_asdu,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    InterrogationCommand,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
    UFrameType,
)
from wind_hub.adapter.outbound.protocol.iec104.connection import (
    ConnectionState,
    ConnectionStateMachine,
)
from wind_hub.adapter.outbound.protocol.iec104.flow import FlowController
from wind_hub.adapter.outbound.protocol.iec104.timers import IEC104Timers
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointValue, Quality

logger = logging.getLogger(__name__)

START_CHAR = 0x68

# TypeIDs that carry measurement values (for cache population).
_MEASUREMENT_TYPE_IDS: frozenset[TypeID] = frozenset(
    {
        TypeID.M_SP_NA_1,
        TypeID.M_SP_TB_1,
        TypeID.M_DP_NA_1,
        TypeID.M_DP_TB_1,
        TypeID.M_ME_NA_1,
        TypeID.M_ME_NB_1,
        TypeID.M_ME_NC_1,
        TypeID.M_ME_TD_1,
        TypeID.M_ME_TF_1,
    }
)


def _extract_value(obj: object) -> object:
    """Extract the measurement value from an info object."""
    for attr in ("value", "measured_value", "normalized_value"):
        val = getattr(obj, attr, None)
        if val is not None:
            return val
    return None


def _extract_quality(obj: object) -> Quality:
    """Extract quality info from an info object — maps QualityFlag → Quality."""
    q = getattr(obj, "quality", None)
    if q is None or not isinstance(q, QualityFlag):
        return Quality.GOOD
    if QualityFlag.IV in q:
        return Quality.BAD
    if QualityFlag.NT in q or QualityFlag.SB in q:
        return Quality.UNCERTAIN
    if QualityFlag.BL in q or QualityFlag.OV in q:
        return Quality.UNCERTAIN
    return Quality.GOOD


class IEC104Session:
    """A single IEC 60870-5-104 TCP session.

    Holds the TCP connection, state machine, flow controller, and
    timers for one slave.

    **Lifecycle**::

        create → start() → [running with bg tasks] → close()
    """

    def __init__(
        self,
        host: str,
        port: int,
        common_addr: int,
        k: int = 12,
        w: int = 8,
        t1: float = 15.0,
        t2: float = 10.0,
        t3: float = 20.0,
    ) -> None:
        self._host = host
        self._port = port
        self._common_addr = common_addr
        self._t1 = t1
        self._t2 = t2
        self._t3 = t3

        # Core machinery.
        self._state = ConnectionStateMachine()
        self._flow = FlowController(k=k, w=w)
        self._timers = IEC104Timers(t1=t1, t2=t2, t3=t3)

        # TCP IO — set during start().
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        # Outbound queue (raw APDU bytes).
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

        # Background tasks.
        self._receive_task: asyncio.Task[object] | None = None
        self._send_task: asyncio.Task[object] | None = None

        # Interrogation tracking.
        self._interrogation_done: asyncio.Event = asyncio.Event()
        self._interrogation_done.set()  # starts as "done"

        # Point-value cache: IOA → PointValue.
        self._point_cache: dict[int, PointValue] = {}
        # IOA → point_id mapping (set externally by the driver).
        self._ioa_to_point_id: dict[int, str] = {}
        # point_id → IOA reverse map.
        self._point_id_to_ioa: dict[str, int] = {}

        # Optional callback for value updates (reserved for step 7b-3).
        self._on_value_update: Callable[[PointValue], None] | None = None

        # ASDU forwarding callback — set by the driver.
        self._on_asdu: Callable[[ASDU], None] | None = None

        # STARTDT handshake synchronization.
        self._startdt_event: asyncio.Event | None = None

        # Shutdown sentinel.
        self._closed = False

    # ==================================================================
    # properties
    # ==================================================================

    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state.state

    @property
    def is_started(self) -> bool:
        """``True`` when data transfer is active."""
        return self._state.is_started

    @property
    def interrogation_complete(self) -> bool:
        """``True`` when station interrogation has finished."""
        return self._interrogation_done.is_set()

    @property
    def point_cache(self) -> dict[int, PointValue]:
        """Snapshot of the IOA → PointValue cache (a copy)."""
        return dict(self._point_cache)

    # ==================================================================
    # point mapping
    # ==================================================================

    def set_points_mapping(
        self,
        ioa_to_point_id: dict[int, str],
        point_id_to_ioa: dict[str, int],
    ) -> None:
        """Install the IOA ↔ point_id lookup tables."""
        self._ioa_to_point_id = dict(ioa_to_point_id)
        self._point_id_to_ioa = dict(point_id_to_ioa)

    def set_on_value_update(
        self,
        callback: Callable[[PointValue], None] | None,
    ) -> None:
        """Set optional callback for value updates."""
        self._on_value_update = callback

    def set_on_asdu(
        self,
        callback: Callable[[ASDU], None] | None,
    ) -> None:
        """Set optional callback for ASDU forwarding.

        When set, every decoded ASDU (after session-internal processing)
        is passed to *callback*.  The driver uses this to dispatch
        spontaneous updates, interrogation data, and remote-control
        responses to subscribers.
        """
        self._on_asdu = callback

    def send_asdu(self, asdu: ASDU) -> None:
        """Enqueue *asdu* as an I-frame for immediate delivery.

        The session wraps the ASDU in an I-frame with the correct
        sequence numbers and enqueues it for the send loop.

        Raises:
            ProtocolError: If the session is not started.
        """
        if not self._state.is_started:
            raise ProtocolError(
                f"IEC104: cannot send ASDU — session is not started "
                f"(state={self._state.state.name})"
            )

        send_seq = self._flow.next_send_seq()
        self._enqueue_frame_nowait(
            encode_i_frame(
                send_seq,
                self._flow.recv_seq_for_ack(),
                encode_asdu(asdu),
            )
        )
        self._timers.start_t1()
        logger.debug(
            "IEC104: enqueued ASDU type=%s cause=%s",
            asdu.type_id.name,
            asdu.cause.name,
        )

    def get_ioa(self, point_id: str) -> int | None:
        """Look up the IOA for a point_id, or ``None``."""
        return self._point_id_to_ioa.get(point_id)

    def get_point_id(self, ioa: int) -> str | None:
        """Look up the point_id for an IOA, or ``None``."""
        return self._ioa_to_point_id.get(ioa)

    # ==================================================================
    # start / close
    # ==================================================================

    async def start(self) -> None:
        """Open TCP connection, perform STARTDT handshake, and launch
        station interrogation.

        Raises:
            ProtocolError: On connection or handshake failure.
        """
        if self._closed:
            raise ProtocolError("Session is closed — create a new one.")

        logger.info(
            "IEC104: connecting to %s:%d (common_addr=%d)",
            self._host,
            self._port,
            self._common_addr,
        )
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._host,
                self._port,
            )
        except OSError as exc:
            raise ProtocolError(
                f"IEC104: TCP connect to {self._host}:{self._port} failed: {exc}"
            ) from exc

        self._state.to_tcp_connected()
        logger.debug("IEC104: TCP connected to %s:%d", self._host, self._port)

        # Start background tasks.
        self._receive_task = asyncio.ensure_future(self._receive_loop())
        self._send_task = asyncio.ensure_future(self._send_loop())

        # Install timer callbacks.
        self._timers.on_t1_timeout = self._on_t1_timeout
        self._timers.on_t2_timeout = self._on_t2_timeout
        self._timers.on_t3_timeout = self._on_t3_timeout

        # STARTDT handshake.
        await self._startdt_handshake()

        # Station interrogation.
        await self._station_interrogation()

    async def close(self) -> None:
        """Gracefully close the session — STOPDT, cancel tasks, close socket."""
        if self._closed:
            return
        self._closed = True

        logger.info("IEC104: closing session to %s:%d", self._host, self._port)

        # Try to send STOPDT if still started.
        if self._state.is_started:
            with contextlib.suppress(ValueError):
                self._state.to_stopped()
            with contextlib.suppress(Exception):
                self._enqueue_frame_nowait(encode_u_frame(UFrameType.STOPDT_ACT))

        # Cancel all timers.
        await self._timers.stop_all()

        # Cancel background tasks.
        for task in (self._receive_task, self._send_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Close TCP.
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None

        if self._state.state != ConnectionState.DISCONNECTED:
            self._state.to_disconnected()
        logger.info("IEC104: session closed for %s:%d", self._host, self._port)

    async def wait_closed(self) -> None:
        """Wait until the receive and send tasks have finished.

        Does **not** swallow ``CancelledError`` — if the awaiting task
        (e.g. the driver monitor loop) is cancelled, the cancellation
        propagates so the caller can shut down promptly.  Swallowing it
        here would leave the caller blocked waiting on a still-running
        background task.
        """
        for task in (self._receive_task, self._send_task):
            if task is not None and not task.done():
                await task

    # ==================================================================
    # enqueue
    # ==================================================================

    def _enqueue_frame_nowait(self, data: bytes) -> None:
        """Enqueue raw APDU bytes for sending (non-blocking)."""
        try:
            self._send_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(
                "IEC104: send queue full (%d), dropping frame",
                self._send_queue.maxsize,
            )

    async def _enqueue_frame(self, data: bytes) -> None:
        """Enqueue raw APDU bytes for sending (may block)."""
        await self._send_queue.put(data)

    # ==================================================================
    # STARTDT handshake
    # ==================================================================

    async def _startdt_handshake(self) -> None:
        """Send STARTDT act and wait for STARTDT con."""
        self._state.to_startdt_pending()
        logger.debug("IEC104: sending STARTDT act to %s", self._host)

        self._startdt_event = asyncio.Event()
        self._enqueue_frame_nowait(encode_u_frame(UFrameType.STARTDT_ACT))

        try:
            await asyncio.wait_for(
                self._startdt_event.wait(),
                timeout=self._t1,
            )
        except TimeoutError:
            raise ProtocolError(f"IEC104: STARTDT handshake timed out after {self._t1}s") from None

        self._startdt_event = None
        self._state.to_started()
        logger.info("IEC104: STARTDT handshake complete for %s", self._host)

    # ==================================================================
    # station interrogation
    # ==================================================================

    async def _station_interrogation(self) -> None:
        """Send C_IC_NA_1 (QOI=20) and wait for ACT_TERM."""
        self._interrogation_done.clear()

        gi_asdu = ASDU(
            type_id=TypeID.C_IC_NA_1,
            cause=CauseOfTransmission.ACTIVATION,
            common_address=self._common_addr,
            objects=[InterrogationCommand(ioa=0)],  # QOI=20 hardcoded in encoder
        )

        logger.info("IEC104: sending station interrogation to %s", self._host)
        send_seq = self._flow.next_send_seq()
        self._enqueue_frame_nowait(
            encode_i_frame(
                send_seq,
                self._flow.recv_seq_for_ack(),
                encode_asdu(gi_asdu),
            )
        )
        self._timers.start_t1()

        # Wait for interrogation to complete (ACT_TERM received).
        try:
            await asyncio.wait_for(
                self._interrogation_done.wait(),
                timeout=self._t1 * 2,  # generous timeout
            )
        except TimeoutError:
            logger.warning(
                "IEC104: station interrogation timed out for %s",
                self._host,
            )
            self._interrogation_done.set()

        logger.info(
            "IEC104: station interrogation complete for %s (%d cached points)",
            self._host,
            len(self._point_cache),
        )

    # ==================================================================
    # background loops
    # ==================================================================

    async def _receive_loop(self) -> None:
        """Continuously read APDU frames from the TCP socket and dispatch."""
        buffer = bytearray()
        reader = self._reader
        assert reader is not None

        while not self._closed:
            try:
                # Read at least 2 bytes (start + length).
                while len(buffer) < 2:
                    chunk = await reader.read(2 - len(buffer))
                    if not chunk:
                        logger.warning(
                            "IEC104: TCP receive EOF from %s",
                            self._host,
                        )
                        await self._handle_disconnect()
                        return
                    buffer.extend(chunk)

                if buffer[0] != START_CHAR:
                    bad = buffer[0]
                    del buffer[0]
                    logger.warning(
                        "IEC104: unexpected start byte %#04x, skipping",
                        bad,
                    )
                    continue

                apdu_len = buffer[1]
                total_len = 2 + apdu_len

                while len(buffer) < total_len:
                    chunk = await reader.read(total_len - len(buffer))
                    if not chunk:
                        logger.warning(
                            "IEC104: TCP receive EOF mid-frame from %s",
                            self._host,
                        )
                        await self._handle_disconnect()
                        return
                    buffer.extend(chunk)

                frame_bytes = bytes(buffer[:total_len])
                del buffer[:total_len]

                # Reset t3 (data received).
                self._timers.cancel_t3()
                self._timers.start_t3()

                frame = decode_apdu(frame_bytes)
                if isinstance(frame, IFrame):
                    await self._handle_i_frame(frame)
                elif isinstance(frame, SFrame):
                    await self._handle_s_frame(frame)
                elif isinstance(frame, UFrame):
                    await self._handle_u_frame(frame)

            except ProtocolError:
                logger.exception(
                    "IEC104: protocol error in receive loop for %s",
                    self._host,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "IEC104: unexpected error in receive loop for %s",
                    self._host,
                )

    async def _send_loop(self) -> None:
        """Continuously drain the send queue and write to TCP."""
        writer = self._writer
        assert writer is not None

        while not self._closed:
            try:
                data = await self._send_queue.get()
                writer.write(data)
                await writer.drain()
                self._send_queue.task_done()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "IEC104: error in send loop for %s",
                    self._host,
                )
                await self._handle_disconnect()
                return

    # ==================================================================
    # frame handlers
    # ==================================================================

    async def _handle_i_frame(self, frame: IFrame) -> None:
        """Process an incoming I-frame."""
        self._flow.on_received()

        # Process peer's acknowledgement (N(R) in frame).
        self._flow.on_ack(frame.recv_seq)
        if not self._flow.ack_is_outstanding():
            self._timers.cancel_t1()

        # Start / restart t2 (ack delay).
        self._timers.start_t2()

        # If we've reached w unacked receives, send S-frame now.
        if self._flow.needs_ack:
            await self._send_s_ack()

        # Decode and process the ASDU.
        try:
            asdu, _ = decode_asdu(frame.asdu)
        except ProtocolError:
            logger.exception(
                "IEC104: failed to decode ASDU from %s",
                self._host,
            )
            return

        await self._process_asdu(asdu)

    async def _handle_s_frame(self, frame: SFrame) -> None:
        """Process an incoming S-frame (acknowledgement)."""
        self._flow.on_ack(frame.recv_seq)
        if not self._flow.ack_is_outstanding():
            self._timers.cancel_t1()

    async def _handle_u_frame(self, frame: UFrame) -> None:
        """Process an incoming U-frame."""
        if frame.frame_type == UFrameType.STARTDT_CON:
            logger.debug("IEC104: received STARTDT con from %s", self._host)
            if self._startdt_event is not None:
                self._startdt_event.set()

        elif frame.frame_type == UFrameType.STARTDT_ACT:
            logger.debug("IEC104: received STARTDT act from %s", self._host)
            self._enqueue_frame_nowait(encode_u_frame(UFrameType.STARTDT_CON))

        elif frame.frame_type == UFrameType.STOPDT_CON:
            logger.debug("IEC104: received STOPDT con from %s", self._host)

        elif frame.frame_type == UFrameType.STOPDT_ACT:
            logger.debug("IEC104: received STOPDT act from %s", self._host)
            self._enqueue_frame_nowait(encode_u_frame(UFrameType.STOPDT_CON))

        elif frame.frame_type == UFrameType.TESTFR_ACT:
            logger.debug("IEC104: received TESTFR act from %s", self._host)
            self._enqueue_frame_nowait(encode_u_frame(UFrameType.TESTFR_CON))

        elif frame.frame_type == UFrameType.TESTFR_CON:
            logger.debug("IEC104: received TESTFR con from %s", self._host)

    # ==================================================================
    # ASDU processing
    # ==================================================================

    async def _process_asdu(self, asdu: ASDU) -> None:
        """Route an incoming ASDU based on its TypeID and COT."""
        # --- Interrogation lifecycle (session-internal) ---
        if asdu.type_id == TypeID.C_IC_NA_1:
            if asdu.cause == CauseOfTransmission.ACTIVATION_CON:
                logger.debug("IEC104: interrogation activation confirmed")
                # Still forward to driver.
                if self._on_asdu is not None:
                    self._on_asdu(asdu)
                return
            if asdu.cause == CauseOfTransmission.ACTIVATION_TERMINATION:
                logger.debug("IEC104: interrogation activation terminated")
                self._interrogation_done.set()
                if self._on_asdu is not None:
                    self._on_asdu(asdu)
                return

        # --- Interrogation data ---
        if asdu.cause == CauseOfTransmission.INTERROGATED_BY_STATION:
            await self._cache_objects(asdu)

        # --- Other measurement data (spontaneous, periodic, etc.) ---
        if asdu.type_id in _MEASUREMENT_TYPE_IDS:
            await self._cache_objects(asdu)

        # --- Forward to driver for subscriber dispatch ---
        if self._on_asdu is not None:
            self._on_asdu(asdu)

    async def _cache_objects(self, asdu: ASDU) -> None:
        """Decode info objects from *asdu* and update the point cache."""
        for obj in asdu.objects:
            ioa: int = getattr(obj, "ioa", 0)
            point_id = self._ioa_to_point_id.get(ioa)
            if point_id is None:
                continue

            value = _extract_value(obj)
            quality = _extract_quality(obj)
            pv = PointValue(
                device_id="",
                point_id=point_id,
                value=value,
                quality=quality,
                timestamp=datetime.now(UTC),
            )
            self._point_cache[ioa] = pv

            if self._on_value_update is not None:
                try:
                    self._on_value_update(pv)
                except Exception:
                    logger.exception(
                        "IEC104: value update callback raised for IOA %d",
                        ioa,
                    )

    # ==================================================================
    # S-frame ack
    # ==================================================================

    async def _send_s_ack(self) -> None:
        """Send an S-frame acknowledgement."""
        s_frame = encode_s_frame(self._flow.recv_seq_for_ack())
        self._enqueue_frame_nowait(s_frame)
        self._flow.on_ack_sent()
        self._timers.cancel_t2()

    # ==================================================================
    # disconnect handler
    # ==================================================================

    async def _handle_disconnect(self) -> None:
        """Called when the TCP connection is lost."""
        if self._state.is_connected:
            with contextlib.suppress(ValueError):
                self._state.to_disconnected()

        self._timers.reset_all()

    # ==================================================================
    # timer callbacks
    # ==================================================================

    async def _on_t1_timeout(self) -> None:
        """t1 expired — peer did not ack. Close connection."""
        logger.warning(
            "IEC104: t1 timeout (%ss) for %s — closing connection",
            self._t1,
            self._host,
        )
        await self._handle_disconnect()

    async def _on_t2_timeout(self) -> None:
        """t2 expired — send S-frame ack."""
        logger.debug("IEC104: t2 timeout — sending S-frame ack to %s", self._host)
        await self._send_s_ack()

    async def _on_t3_timeout(self) -> None:
        """t3 expired — no data received, send TESTFR act."""
        logger.debug("IEC104: t3 timeout — sending TESTFR to %s", self._host)
        self._enqueue_frame_nowait(encode_u_frame(UFrameType.TESTFR_ACT))
        self._timers.start_t1()
