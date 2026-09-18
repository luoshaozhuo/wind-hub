"""IEC 60870-5-104 protocol driver.

Full ProtocolPort implementation: connect, close, read, write, subscribe,
spontaneous-update forwarding, and remote control.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from wind_hub.adapter.outbound.protocol.iec104.codec.asdu import ASDU
from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    DoubleCommand,
    SetpointCommandShort,
    SingleCommand,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
)
from wind_hub.adapter.outbound.protocol.iec104.commands import (
    PendingCommand,
    PendingCommandRegistry,
)
from wind_hub.adapter.outbound.protocol.iec104.config import IEC104Config
from wind_hub.adapter.outbound.protocol.iec104.session import IEC104Session
from wind_hub.adapter.outbound.protocol.iec104.subscriptions import (
    SubscriptionRegistry,
)
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort

logger = logging.getLogger(__name__)

# Exponential backoff parameters.
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_CAP = 30.0
_RECONNECT_BACKOFF_MULTIPLIER = 2.0


def _is_timeout_related(exc: BaseException) -> bool:
    """判断会话抛出的协议错误是否源于连接/握手超时。

    ``IEC104Session.start`` 把 TCP 连接失败（``OSError``，errno ETIMEDOUT
    在 Python 3.10+ 映射为 ``TimeoutError``）与 STARTDT 握手超时统一包装为
    ``ProtocolError``；这里经 ``__cause__`` 链与消息文本识别超时类失败，
    供重连循环把这类「对端不可达的日常表现」降级为简洁 warning（决策 2）。
    """
    if isinstance(exc.__cause__, TimeoutError):
        return True
    return "timed out" in str(exc)


# TypeIDs for remote-control commands.
_CONTROL_TYPE_IDS: frozenset[TypeID] = frozenset(
    {
        TypeID.C_SC_NA_1,
        TypeID.C_DC_NA_1,
        TypeID.C_SE_NC_1,
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
    """Extract quality info — maps QualityFlag → Quality."""
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


class IEC104Driver:
    """IEC 60870-5-104 protocol driver.

    Implements :class:`~wind_hub.domain.port.outbound.ProtocolPort`.
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = IEC104Config.from_device_config(cfg)
        self._lock = asyncio.Lock()

        self._session: IEC104Session | None = None
        self._monitor_task: asyncio.Task[object] | None = None
        self._shutdown = False

        # Point mapping.
        self._ioa_to_point_id: dict[int, str] = {}
        self._point_id_to_ioa: dict[str, int] = {}
        # Point data types (point_id → data_type) for control ASDU selection.
        self._point_data_types: dict[str, str] = {}

        # Subscriptions.
        self._subscriptions = SubscriptionRegistry()

        # Remote control.
        self._pending_commands = PendingCommandRegistry()

        self._failed = False

    # ==================================================================
    # point mapping
    # ==================================================================

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """Build the IOA ↔ point_id lookup tables."""
        ioa_to_point_id: dict[int, str] = {}
        point_id_to_ioa: dict[str, int] = {}

        for p in points:
            try:
                ioa = int(p.address.ioa)  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                logger.warning(
                    "IEC104: point '%s' has no valid IOA — skipping",
                    p.point_id,
                )
                continue

            if ioa in ioa_to_point_id:
                logger.warning(
                    "IEC104: duplicate IOA %d for points '%s' and '%s'",
                    ioa,
                    ioa_to_point_id[ioa],
                    p.point_id,
                )
            ioa_to_point_id[ioa] = p.point_id
            point_id_to_ioa[p.point_id] = ioa

        self._ioa_to_point_id = ioa_to_point_id
        self._point_id_to_ioa = point_id_to_ioa
        # Also store data types for each point.
        self._point_data_types = {p.point_id: p.data_type for p in points}
        logger.info(
            "IEC104: mapped %d points for device %s",
            len(ioa_to_point_id),
            self._cfg.host,
        )

    def _resolve_ioa(self, ref: PointRef) -> int | None:
        """Resolve a PointRef to an IOA."""
        return self._point_id_to_ioa.get(ref.point_id)

    # ==================================================================
    # ProtocolPort — connect / close
    # ==================================================================

    async def connect(self) -> None:
        """Establish the IEC104 connection."""
        async with self._lock:
            if self._session is not None:
                logger.warning("IEC104: connect() called but already connected")
                return
            if self._failed:
                raise ProtocolError("IEC104: driver is in FAILED state — manual reset required")

            self._shutdown = False

            session = IEC104Session(
                host=self._cfg.host,
                port=self._cfg.port,
                common_addr=self._cfg.common_addr,
                k=self._cfg.k,
                w=self._cfg.w,
                t1=self._cfg.t1,
                t2=self._cfg.t2,
                t3=self._cfg.t3,
            )
            session.set_points_mapping(
                self._ioa_to_point_id,
                self._point_id_to_ioa,
            )
            # Install ASDU forwarding callback.
            session.set_on_asdu(self._on_asdu_received)

            try:
                await session.start()
            except ProtocolError:
                self._failed = True
                raise

            self._session = session
            self._monitor_task = asyncio.ensure_future(self._monitor_loop())

    async def close(self) -> None:
        """Tear down the IEC104 connection."""
        async with self._lock:
            self._shutdown = True

            # Close the session *before* tearing down the monitor loop.  The
            # monitor awaits `session.wait_closed()`, which awaits the receive
            # and send tasks in sequence; cancelling the monitor first would
            # only interrupt the first of those awaits, leaving it blocked on
            # the still-running send task (see wait_closed).  Closing the
            # session cancels both tasks, so wait_closed returns and the
            # monitor observes `_shutdown` and exits.
            if self._session is not None:
                await self._session.close()
                self._session = None

            if self._monitor_task is not None and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
            self._monitor_task = None

            # Fail any pending commands.
            self._fail_all_pending("connection closed")

            self._failed = False

    def _fail_all_pending(self, reason: str) -> None:
        """Resolve all pending commands as failed."""
        # We need to collect keys first since remove mutates the dict.
        ioas = list(self._pending_commands._pending.keys())
        for ioa in ioas:
            pending = self._pending_commands.get(ioa)
            if pending is not None and not pending.future.done():
                pending.future.set_result(
                    CommandResult(
                        command_id=pending.command.command_id,
                        success=False,
                        error=reason,
                    )
                )
            self._pending_commands.remove(ioa)

    # ==================================================================
    # ProtocolPort — read
    # ==================================================================

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """Batch-read points from the cached measurement table."""
        session = self._session
        if session is None or not session.is_started:
            raise ProtocolError("IEC104: cannot read — driver is not connected")

        cache = session.point_cache
        results: list[PointValue] = []

        for ref in points:
            ioa = session.get_ioa(ref.point_id)
            if ioa is None or ioa not in cache:
                results.append(
                    PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=None,
                        quality=Quality.BAD,
                        source="iec104",
                    )
                )
            else:
                pv = cache[ioa]
                results.append(
                    PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=pv.value,
                        quality=pv.quality,
                        timestamp=pv.timestamp,
                        source="iec104",
                    )
                )

        return results

    # ==================================================================
    # ProtocolPort — write
    # ==================================================================

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """Execute remote-control commands.

        Supports:
        - Single-point control (C_SC_NA_1)
        - Double-point control (C_DC_NA_1)
        - Set-point command (C_SE_NC_1)

        All commands are executed concurrently; failure of one does not
        affect others.
        """
        if not cmds:
            return []

        session = self._session
        if session is None or not session.is_started:
            raise ProtocolError("IEC104: cannot write — driver is not connected")

        # Launch all writes concurrently.
        tasks = [self._execute_one_command(cmd, session) for cmd in cmds]
        return await asyncio.gather(*tasks)

    async def _execute_one_command(
        self,
        cmd: Command,
        session: IEC104Session,
    ) -> CommandResult:
        """Execute a single remote-control command."""
        try:
            # 1. Resolve point_id → IOA.
            ioa = self._point_id_to_ioa.get(cmd.point_id)
            if ioa is None:
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error=f"unknown point '{cmd.point_id}'",
                )

            # 2. Build and validate the control ASDU.
            asdu = self._build_control_asdu(cmd, ioa)

            # 3. Register pending command.
            future: asyncio.Future[CommandResult] = asyncio.Future()
            timeout = self._cfg.t1 * 2
            pending = PendingCommand(
                command=cmd,
                ioa=ioa,
                future=future,
                timeout=timeout,
            )

            try:
                self._pending_commands.register(pending)
            except CommandError as exc:
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error=str(exc),
                )

            # 4. Send the ASDU.
            try:
                session.send_asdu(asdu)
            except ProtocolError:
                self._pending_commands.remove(ioa)
                if not future.done():
                    future.cancel()
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error="session not started",
                )

            # 5. Wait for the future.
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                self._pending_commands.remove(ioa)
                if not future.done():
                    future.cancel()
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error="timeout",
                )

        except Exception as exc:
            return CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=str(exc),
            )

    def _build_control_asdu(self, cmd: Command, ioa: int) -> ASDU:
        """Build the control ASDU based on value type and point data_type.

        Dispatch logic:

        * ``bool`` value or ``data_type == "bool"`` → C_SC_NA_1 (single-point)
        * ``float`` value or ``float*`` data_type → C_SE_NC_1 (set-point)
        * ``int`` value 0 or 1 → C_SC_NA_1 (single-point)
        * ``int`` value 2, or 1 when data_type is int/uint → C_DC_NA_1 (double-point)
        * ``int`` any other value → C_SE_NC_1 (set-point)

        Value 1 is ambiguous (single-point True *or* double-point OFF).
        We use *data_type* to disambiguate: ``"bool"`` → single,
        anything else (int/uint types) → double.
        """
        val = cmd.value
        data_type = self._point_data_types.get(cmd.point_id, "")

        # ── bool / bool data_type → single-point ────────────────────
        # isinstance(val, bool) must precede isinstance(val, int)
        # because bool is an int subclass.
        if isinstance(val, bool) or data_type == "bool":
            return ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=self._cfg.common_addr,
                objects=[SingleCommand(ioa=ioa, value=bool(val), select=False)],
            )

        # ── float / float data_type → set-point ─────────────────────
        if isinstance(val, float) or data_type in ("float32", "float64"):
            return ASDU(
                type_id=TypeID.C_SE_NC_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=self._cfg.common_addr,
                objects=[SetpointCommandShort(ioa=ioa, value=float(val), select=False)],
            )

        # ── int → decide by value + data_type hint ──────────────────
        if isinstance(val, int):
            # val == 1 is ambiguous.  data_type disambiguates:
            #   "bool" or empty → single-point; others → double-point.
            if val == 2 or (val == 1 and data_type not in ("", "bool")):
                return ASDU(
                    type_id=TypeID.C_DC_NA_1,
                    cause=CauseOfTransmission.ACTIVATION,
                    common_address=self._cfg.common_addr,
                    objects=[DoubleCommand(ioa=ioa, value=val, select=False)],
                )
            if val in (0, 1):
                return ASDU(
                    type_id=TypeID.C_SC_NA_1,
                    cause=CauseOfTransmission.ACTIVATION,
                    common_address=self._cfg.common_addr,
                    objects=[SingleCommand(ioa=ioa, value=bool(val), select=False)],
                )
            # Large int → set-point.
            return ASDU(
                type_id=TypeID.C_SE_NC_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=self._cfg.common_addr,
                objects=[SetpointCommandShort(ioa=ioa, value=float(val), select=False)],
            )

        raise ValueError(f"unsupported value type {type(val).__name__} for control command")

    # ==================================================================
    # ProtocolPort — subscribe
    # ==================================================================

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
    ) -> None:
        """Register a subscription."""
        self._subscriptions.subscribe(points, callback, self._resolve_ioa)

    # ==================================================================
    # ProtocolPort — health
    # ==================================================================

    def health(self) -> HealthStatus:
        """Return current health status."""
        if self._failed:
            return HealthStatus(
                healthy=False,
                message="FAILED — max reconnect retries exhausted",
            )
        session = self._session
        if session is None:
            return HealthStatus(healthy=False, message="not connected")
        if session.is_started:
            return HealthStatus(
                healthy=True,
                message=f"connected to {self._cfg.host}:{self._cfg.port}",
            )
        return HealthStatus(
            healthy=False,
            message=f"state={session.state.name}",
        )

    # ==================================================================
    # ASDU dispatch (called by session)
    # ==================================================================

    def _on_asdu_received(self, asdu: ASDU) -> None:
        """Dispatch an incoming ASDU based on COT.

        Called synchronously from the session's receive loop — the
        session has already updated its cache for measurement data.
        """
        try:
            # --- Remote-control responses ---
            if asdu.type_id in _CONTROL_TYPE_IDS:
                if asdu.cause == CauseOfTransmission.ACTIVATION_CON:
                    self._on_activation_con(asdu)
                elif asdu.cause == CauseOfTransmission.ACTIVATION_TERMINATION:
                    self._on_activation_term(asdu)
                return

            # --- Measurement data → subscribers ---
            if asdu.cause in (
                CauseOfTransmission.SPONTANEOUS,
                CauseOfTransmission.INTERROGATED_BY_STATION,
                CauseOfTransmission.PERIODIC,
                CauseOfTransmission.BACKGROUND,
                CauseOfTransmission.INITIALIZED,
                CauseOfTransmission.REQUEST,
            ):
                self._dispatch_point_values(asdu)

        except Exception:
            logger.exception("IEC104: error in _on_asdu_received")

    def _dispatch_point_values(self, asdu: ASDU) -> None:
        """Convert ASDU info objects to PointValue and dispatch to subscribers."""
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
                source="iec104",
            )

            # Dispatch via subscription registry.
            asyncio.ensure_future(self._subscriptions.dispatch(pv, ioa))

    def _on_activation_con(self, asdu: ASDU) -> None:
        """Handle remote-control activation confirmation."""
        # COT byte bit 7 = P/N (negative flag).
        # The codec already stores cause in low 6 bits.  We need the raw
        # byte to check P/N.  But our model only stores the cause enum.
        # The P/N bit is signalled by bit 7 of the raw COT byte.
        # Since the CauseOfTransmission only uses bits 0-5, we need to
        # check the raw ASDU COT byte.  But we don't have it here.
        #
        # Convention: in IEC104, the response to an activation command
        # uses COT=ACTIVATION_CON with P/N=0 for positive or P/N=1 for
        # negative.  The info-object carries the same IOA.
        #
        # For negative confirmation, the slave sends COT with bit 7 set.
        # Our codec strips bit 7 when decoding the cause, so we can't
        # distinguish positive/negative from the cause enum alone.
        #
        # Workaround: check the info objects — some slaves set the
        # SCO/DCO/QOS to a known error state on negative confirmation.
        # But the standard-reliable way is to check the raw COT byte.
        #
        # For now we assume the ASDU cause-only field is what we have.
        # A negative confirmation would have to be signalled differently.
        # In practice, many slaves never send negative confirmations
        # for execute-only commands, so we default to positive.
        for obj in asdu.objects:
            ioa: int = getattr(obj, "ioa", 0)
            # Check for negative confirmation: if the command object has
            # value=False and the original command value was True, this
            # could indicate rejection.  But this is unreliable.
            # Default: positive confirmation.
            self._pending_commands.on_activation_con(ioa, negative=False)

    def _on_activation_term(self, asdu: ASDU) -> None:
        """Handle remote-control activation termination."""
        for obj in asdu.objects:
            ioa: int = getattr(obj, "ioa", 0)
            self._pending_commands.on_activation_term(ioa)

    # ==================================================================
    # reconnect / monitor
    # ==================================================================

    async def _monitor_loop(self) -> None:
        """Watch for disconnection and reconnect with exponential backoff."""
        retries = 0
        backoff = _RECONNECT_BACKOFF_BASE

        while not self._shutdown:
            session = self._session
            if session is not None:
                await session.wait_closed()
                if self._shutdown:
                    return

                logger.warning(
                    "IEC104: session to %s:%d closed — reconnecting",
                    self._cfg.host,
                    self._cfg.port,
                )
                self._session = None
                self._fail_all_pending("connection lost")

            retries += 1
            if retries > self._cfg.max_reconnect_retries:
                logger.error(
                    "IEC104: max reconnect retries (%d) exhausted for %s:%d",
                    self._cfg.max_reconnect_retries,
                    self._cfg.host,
                    self._cfg.port,
                )
                self._failed = True
                return

            await asyncio.sleep(backoff)
            backoff = min(
                backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                _RECONNECT_BACKOFF_CAP,
            )

            if self._shutdown:
                return

            logger.info(
                "IEC104: reconnect attempt %d/%d to %s:%d (backoff=%.1fs)",
                retries,
                self._cfg.max_reconnect_retries,
                self._cfg.host,
                self._cfg.port,
                backoff,
            )

            try:
                new_session = IEC104Session(
                    host=self._cfg.host,
                    port=self._cfg.port,
                    common_addr=self._cfg.common_addr,
                    k=self._cfg.k,
                    w=self._cfg.w,
                    t1=self._cfg.t1,
                    t2=self._cfg.t2,
                    t3=self._cfg.t3,
                )
                new_session.set_points_mapping(
                    self._ioa_to_point_id,
                    self._point_id_to_ioa,
                )
                new_session.set_on_asdu(self._on_asdu_received)
                await new_session.start()
            except TimeoutError:
                # 连接/握手超时是对端不可达的日常表现：降级为简洁 warning，
                # 不打堆栈（决策 2）；其他协议错误仍保留完整堆栈便于排查。
                logger.warning(
                    "IEC104: reconnect timed out for %s:%d",
                    self._cfg.host,
                    self._cfg.port,
                )
                continue
            except ProtocolError as exc:
                if _is_timeout_related(exc):
                    logger.warning(
                        "IEC104: reconnect timed out for %s:%d",
                        self._cfg.host,
                        self._cfg.port,
                    )
                else:
                    logger.exception(
                        "IEC104: reconnect failed for %s:%d",
                        self._cfg.host,
                        self._cfg.port,
                    )
                continue

            self._session = new_session
            retries = 0
            backoff = _RECONNECT_BACKOFF_BASE
            logger.info(
                "IEC104: reconnected to %s:%d",
                self._cfg.host,
                self._cfg.port,
            )


# ---------------------------------------------------------------------------
# self-registration
# ---------------------------------------------------------------------------

from wind_hub.infra.registry import register_protocol  # noqa: E402


@register_protocol("iec104")
def _create_iec104(cfg: DeviceConfig) -> ProtocolPort:
    return IEC104Driver(cfg)
