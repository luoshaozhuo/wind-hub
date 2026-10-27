"""IEC104 协议适配器：业务关联与状态所有者，所有网络报文由 Session 接收。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hat.drivers import iec104

from core.application.errors import (
    ConfigError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
    ProtocolError,
)
from core.application.port.protocol import ListenHandle, ProtocolSampleCallback, SubscriptionHandle
from core.application.protocol_contract import (
    ConnectionHealth,
    InterrogationRequest,
    ProtocolCapability,
    ProtocolLifecycleState,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .codec import build_command, decode_sample, matches_monitoring_type
from .config import parse_iec104_config
from .listener import _Listener
from .mapping import IEC104Point, build_iec104_index, validate_iec104_write_type
from .session import _IEC104Session

logger = logging.getLogger(__name__)
Address = tuple[int, int]


@dataclass
class _Read:
    point: IEC104Point
    result: asyncio.Future[ProtocolSample]


@dataclass
class _Control:
    result: asyncio.Future[iec104.CommandMsg]
    phase: str
    select: bool
    expected_command: object
    accepted: bool = False


@dataclass
class _Interrogation:
    result: asyncio.Future[None]
    kind: str
    group: int | None
    by_address: dict[Address, IEC104Point]
    accepted: bool = False
    samples: dict[Address, ProtocolSample] = field(default_factory=dict)


class IEC104InterrogationIncompleteError(ProtocolError):
    def __init__(self, ca: int, samples: tuple[ProtocolSample, ...], reason: str):
        super().__init__(f"IEC104 interrogation ca={ca} incomplete: {reason}")
        self.common_address = ca
        self.samples = samples
        self.reason = reason
        self.completed = False


class IEC104Driver:
    """IEC104 主站。单 Session，按完整地址和站召区域关联响应。"""

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ):
        if point_table.protocol.name != "iec104":
            raise ConfigError("IEC104 requires IEC104 point table")
        self._config = parse_iec104_config(endpoint, protocol_options)
        self._device_tz = ZoneInfo(self._config.timezone) if self._config.timezone else None
        self._by_id: dict[str, IEC104Point] = {}
        self._by_address: dict[Address, IEC104Point] = {}
        self._health_point_id: str | None = None
        self._state = ProtocolLifecycleState.DISCONNECTED
        self.update_point_table(point_table)
        self._lifecycle = asyncio.Lock()
        self._session: _IEC104Session | None = None
        self._read: dict[Address, _Read] = {}
        self._control: dict[Address, _Control] = {}
        self._unsettled_controls: dict[Address, object] = {}
        self._interrogation: dict[int, _Interrogation] = {}
        self._locks: dict[Address, asyncio.Lock] = {}
        self._station_locks: dict[int, asyncio.Lock] = {}
        self._listeners: set[_Listener] = set()

    @property
    def state(self) -> ProtocolLifecycleState:
        return self._state

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.INTERROGATE,
                ProtocolCapability.LISTEN,
            }
        )

    def update_point_table(self, point_table: PointTable) -> None:
        if self._state is not ProtocolLifecycleState.DISCONNECTED:
            raise ProtocolError("IEC104 point table update requires a closed connection")
        if point_table.protocol.name != "iec104":
            raise ConfigError("IEC104 point table protocol mismatch")
        by_id, by_address = build_iec104_index(list(point_table.points.values()))
        readable = (
            point.point_id
            for point in by_id.values()
            if point.type_id is not None and point.type_id.startswith("M_")
        )
        configured = self._config.health_point
        if configured is not None:
            configured_point = by_id.get(configured)
            if (
                configured_point is None
                or configured_point.type_id is None
                or not configured_point.type_id.startswith("M_")
            ):
                raise ConfigError(f"IEC104 health point '{configured}' is not readable")
            health_point_id = configured
        else:
            health_point_id = next(readable, None)
        self._by_id, self._by_address = by_id, by_address
        self._health_point_id = health_point_id

    def is_open(self) -> bool:
        return self._session is not None and self._session.is_open

    async def connect(self) -> None:
        async with self._lifecycle:
            if self.is_open():
                return
            if self._state is ProtocolLifecycleState.FAULTED:
                raise ProtocolConnectionError("IEC104 driver requires successful cleanup")
            if self._session is not None:
                raise ProtocolConnectionError("IEC104 previous session must close first")
            self._state = ProtocolLifecycleState.CONNECTING
            c = self._config
            session: _IEC104Session | None = None
            try:
                session = _IEC104Session(
                    c.host,
                    c.port,
                    t1=c.t1,
                    t2=c.t2,
                    t3=c.t3,
                    k=c.k,
                    w=c.w,
                    on_message=lambda msg: self._dispatch(session, msg),
                    on_disconnect=lambda exc: self._disconnect(session, exc),
                )
                self._session = session
                await asyncio.wait_for(session.open(), timeout=c.t0)
                if not session.is_open:
                    raise ProtocolConnectionError("IEC104 session lost during STARTDT")
            except BaseException as exc:
                await self._close_locked()
                if isinstance(exc, OSError | TimeoutError):
                    raise ProtocolConnectionError(
                        f"IEC104 connection failed: {exc or type(exc).__name__}"
                    ) from exc
                raise
            self._state = ProtocolLifecycleState.CONNECTED

    async def close(self) -> None:
        async with self._lifecycle:
            await self._close_locked()

    async def _close_locked(self) -> None:
        """Release the owned session; caller holds the lifecycle lock."""
        self._state = ProtocolLifecycleState.CLOSING
        self._fail_all(ProtocolConnectionError("IEC104 session closed"))
        try:
            if self._session is not None:
                await self._session.close()
                self._session = None
            for listener in tuple(self._listeners):
                await listener.close()
        except BaseException:
            self._state = ProtocolLifecycleState.FAULTED
            raise
        self._state = ProtocolLifecycleState.DISCONNECTED

    def _disconnect(self, session: _IEC104Session | None, exc: BaseException) -> None:
        if session is not self._session:
            return
        self._fail_all(ProtocolConnectionError(f"IEC104 connection lost: {exc}"))
        self._state = ProtocolLifecycleState.CLOSING

    def _fail_all(self, exc: Exception) -> None:
        futures = (
            [x.result for x in self._read.values()]
            + [x.result for x in self._control.values()]
            + [x.result for x in self._interrogation.values()]
        )
        for future in futures:
            if not future.done():
                future.set_exception(exc)
        self._read.clear()
        self._control.clear()
        self._unsettled_controls.clear()
        self._interrogation.clear()

    async def _send(self, session: _IEC104Session, message: object) -> None:
        if session is not self._session:
            raise ProtocolConnectionError("IEC104 session changed")
        await session.send([message])

    async def _isolate(self, session: _IEC104Session) -> None:
        if session is not self._session:
            return
        async with self._lifecycle:
            if session is self._session:
                await self._close_locked()

    def _point(self, key: str) -> IEC104Point:
        try:
            return self._by_id[key]
        except KeyError as exc:
            raise ConfigError(f"IEC104 point not configured: {key}") from exc

    async def health(self) -> ConnectionHealth:
        if not self.is_open():
            return ConnectionHealth(False, "IEC104 session unavailable")
        if any(listener.failed or listener.task.done() for listener in self._listeners):
            return ConnectionHealth(False, "IEC104 listener stopped; acquisition recovery required")
        if self._health_point_id is None:
            return ConnectionHealth(False, "no readable IEC104 point available for health check")
        try:
            await self.read_one(self._health_point_id)
        except Exception as exc:
            return ConnectionHealth(False, str(exc))
        return ConnectionHealth(True)

    async def read_one(self, point_id: str) -> ProtocolSample:
        point = self._point(point_id)
        if point.type_id is None or not point.type_id.startswith("M_"):
            raise ConfigError("active read requires an explicit monitoring M_* Type ID")
        session = self._require_session()
        key = (point.common_address, point.ioa)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if session is not self._session:
                raise ProtocolConnectionError("IEC104 session changed")
            future: asyncio.Future[ProtocolSample] = asyncio.get_running_loop().create_future()
            self._read[key] = _Read(point, future)
            try:
                await self._send(
                    session,
                    iec104.ReadMsg(
                        is_test=False,
                        originator_address=0,
                        asdu_address=point.common_address,
                        io_address=point.ioa,
                        cause=iec104.ReadReqCause.REQUEST,
                    ),
                )
                return await asyncio.wait_for(future, timeout=self._config.read_timeout)
            except (TimeoutError, ProtocolConnectionError, ProtocolError, asyncio.CancelledError):
                await asyncio.shield(self._isolate(session))
                raise
            finally:
                self._read.pop(key, None)

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        del point_ids
        raise ProtocolCapabilityError("IEC104 does not support native batch reads")

    def _require_session(self) -> _IEC104Session:
        if not self.is_open():
            raise ProtocolConnectionError("IEC104 session unavailable")
        assert self._session is not None
        return self._session

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        point = self._point(write.point)
        try:
            validate_iec104_write_type(point)
            command = build_command(point, write.value)
        except (TypeError, ValueError, ConfigError) as exc:
            return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_FAILURE, str(exc))
        session = self._require_session()
        key = (point.common_address, point.ioa)
        lock = self._locks.setdefault(key, asyncio.Lock())
        station_lock = self._station_locks.setdefault(point.common_address, asyncio.Lock())
        async with station_lock, lock:
            if key in self._unsettled_controls:
                return ProtocolWriteResult(
                    write.point,
                    WriteOutcome.CONFIRMED_FAILURE,
                    "previous ACT_CON confirmed, ACT_TERM still pending; command not sent",
                )
            try:
                selected: object | None = None
                if point.command_mode == "select_execute":
                    selected = command._replace(select=True)
                    accepted = await self._control_phase(
                        session, point, selected, "select", "act_con"
                    )
                    if not accepted:
                        return ProtocolWriteResult(
                            write.point, WriteOutcome.CONFIRMED_FAILURE, "SELECT rejected"
                        )
                accepted = await self._control_phase(
                    session, point, command, "execute", point.confirmation
                )
                return ProtocolWriteResult(
                    write.point,
                    WriteOutcome.CONFIRMED_SUCCESS if accepted else WriteOutcome.CONFIRMED_FAILURE,
                )
            except (TimeoutError, ProtocolError, asyncio.CancelledError) as exc:
                await asyncio.shield(self._isolate(session))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                reason = exc or type(exc).__name__
                return ProtocolWriteResult(
                    write.point,
                    WriteOutcome.UNKNOWN,
                    f"IEC104 write outcome unknown; command may have executed: {reason}",
                )

    async def _control_phase(
        self,
        session: _IEC104Session,
        point: IEC104Point,
        command: object,
        phase: str,
        confirmation: str,
    ) -> bool:
        key = (point.common_address, point.ioa)
        future: asyncio.Future[iec104.CommandMsg] = asyncio.get_running_loop().create_future()
        self._control[key] = _Control(
            future, confirmation, bool(getattr(command, "select", False)), command
        )
        if phase == "execute" and confirmation == "act_con":
            self._unsettled_controls[key] = command
        try:
            await self._send(
                session,
                iec104.CommandMsg(
                    is_test=False,
                    originator_address=0,
                    asdu_address=point.common_address,
                    io_address=point.ioa,
                    command=command,
                    is_negative_confirm=False,
                    time=None,
                    cause=iec104.CommandReqCause.ACTIVATION,
                ),
            )
            response = await asyncio.wait_for(future, timeout=self._config.command_timeout)
            if (
                phase == "execute"
                and response.cause == iec104.CommandResCause.ACTIVATION_TERMINATION
                and response.is_negative_confirm
            ):
                raise ProtocolError("IEC104 execute terminated negatively; outcome unknown")
            rejected = response.is_negative_confirm or response.cause in (
                iec104.CommandResCause.UNKNOWN_TYPE,
                iec104.CommandResCause.UNKNOWN_CAUSE,
                iec104.CommandResCause.UNKNOWN_ASDU_ADDRESS,
                iec104.CommandResCause.UNKNOWN_IO_ADDRESS,
            )
            if rejected:
                self._unsettled_controls.pop(key, None)
            return not rejected
        finally:
            self._control.pop(key, None)

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        del writes
        raise ProtocolCapabilityError("IEC104 has no native atomic batch write")

    async def interrogate(self, request: InterrogationRequest) -> tuple[ProtocolSample, ...]:
        addresses = (
            (request.common_address,)
            if request.common_address is not None
            else tuple(sorted({point.common_address for point in self._by_id.values()}))
        )
        results: list[ProtocolSample] = []
        for ca in addresses:
            session = self._require_session()
            lock = self._station_locks.setdefault(ca, asyncio.Lock())
            async with lock:
                future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                pending = _Interrogation(
                    future, request.kind, request.group, dict(self._by_address)
                )
                self._interrogation[ca] = pending
                try:
                    if request.kind == "station":
                        message = iec104.InterrogationMsg(
                            is_test=False,
                            originator_address=0,
                            asdu_address=ca,
                            request=20 if request.group is None else 20 + request.group,
                            is_negative_confirm=False,
                            cause=iec104.CommandReqCause.ACTIVATION,
                        )
                    else:
                        message = iec104.CounterInterrogationMsg(
                            is_test=False,
                            originator_address=0,
                            asdu_address=ca,
                            request=5 if request.group is None else request.group,
                            freeze=iec104.FreezeCode.READ,
                            is_negative_confirm=False,
                            cause=iec104.CommandReqCause.ACTIVATION,
                        )
                    await self._send(session, message)
                    await asyncio.wait_for(future, timeout=self._config.t1 * 2)
                    results.extend(pending.samples.values())
                except (TimeoutError, ProtocolError, asyncio.CancelledError) as exc:
                    await asyncio.shield(self._isolate(session))
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise IEC104InterrogationIncompleteError(
                        ca, (*results, *pending.samples.values()), str(exc)
                    ) from exc
                finally:
                    self._interrogation.pop(ca, None)
        return tuple(results)

    def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        del point_ids, callback, interval
        raise ProtocolCapabilityError("IEC104 does not support remote subscriptions")

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        del interval
        self._require_session()
        selected = frozenset(point_ids or self._by_id)
        for point_id in selected:
            self._point(point_id)
        listener = _Listener(self, selected, callback)
        self._listeners.add(listener)
        return listener

    def _dispatch(self, session: _IEC104Session | None, messages: object) -> None:
        if session is not self._session or session is None:
            return
        if not isinstance(messages, tuple | list):
            raise ProtocolError("IEC104 receive callback must deliver a message batch")
        samples: list[ProtocolSample] = []
        received_at = datetime.now(UTC)
        for msg in messages:
            try:
                if isinstance(msg, iec104.DataMsg):
                    sample = self._data(msg, received_at)
                    if sample is not None:
                        samples.append(sample)
                elif isinstance(msg, iec104.CommandMsg):
                    self._command(msg)
                elif isinstance(msg, iec104.InterrogationMsg | iec104.CounterInterrogationMsg):
                    self._interrogation_reply(msg)
                elif isinstance(msg, iec104.ReadMsg):
                    key = (msg.asdu_address, msg.io_address)
                    req = self._read.get(key)
                    if (
                        req is not None
                        and not req.result.done()
                        and not msg.is_test
                        and getattr(msg.cause, "value", msg.cause) in (44, 45, 46, 47)
                    ):
                        req.result.set_exception(
                            ProtocolError(f"IEC104 read rejected: {msg.cause}")
                        )
            except Exception:
                logger.exception("IEC104 invalid incoming message dropped")
        if samples:
            batch = tuple(samples)
            for listener in tuple(self._listeners):
                listener.offer(batch)

    def _data(self, msg: iec104.DataMsg, received_at: datetime) -> ProtocolSample | None:
        if msg.is_test:
            return None
        key = (msg.asdu_address, msg.io_address)
        cause = msg.cause
        read = self._read.get(key)
        if read is not None and cause == iec104.DataResCause.REQUEST:
            if not matches_monitoring_type(read.point.type_id, msg):
                if not read.result.done():
                    read.result.set_exception(ProtocolError("IEC104 active read Type ID mismatch"))
            else:
                try:
                    received = decode_sample(
                        read.point.point_id,
                        msg,
                        receive_time=received_at,
                        device_tz=self._device_tz,
                    )
                except (ValueError, TypeError) as exc:
                    if not read.result.done():
                        read.result.set_exception(
                            ProtocolError(f"IEC104 read decode failed: {exc}")
                        )
                else:
                    if not read.result.done():
                        read.result.set_result(received)

        gi = self._interrogation.get(msg.asdu_address)
        gi_point: IEC104Point | None = None
        if gi is not None and gi.accepted and self._matches_cause(gi, cause):
            gi_point = gi.by_address.get(key)
            if gi_point is not None and matches_monitoring_type(gi_point.type_id, msg):
                try:
                    gi.samples[key] = decode_sample(
                        gi_point.point_id, msg, receive_time=received_at, device_tz=self._device_tz
                    )
                except (ValueError, TypeError):
                    logger.warning("IEC104 interrogation sample decode failed: %s", key)

        point = self._by_address.get(key)
        if point is None or not matches_monitoring_type(point.type_id, msg):
            return None
        if (
            read is not None
            and cause == iec104.DataResCause.REQUEST
            and point.point_id != read.point.point_id
        ):
            return None
        if gi_point is not None and point.point_id != gi_point.point_id:
            return None
        try:
            return decode_sample(
                point.point_id,
                msg,
                receive_time=received_at,
                device_tz=self._device_tz,
            )
        except (ValueError, TypeError):
            logger.warning("IEC104 unsupported point data: %s", key)
            return None

    @staticmethod
    def _matches_cause(pending: _Interrogation, cause: object) -> bool:
        if pending.kind == "station":
            expected = 20 if pending.group is None else 20 + pending.group
            return getattr(cause, "value", cause) == expected
        expected = 37 if pending.group is None else 37 + pending.group
        return getattr(cause, "value", cause) == expected

    def _command(self, msg: iec104.CommandMsg) -> None:
        key = (msg.asdu_address, msg.io_address)
        if not msg.is_test and msg.cause == iec104.CommandResCause.ACTIVATION_TERMINATION:
            previous = self._unsettled_controls.get(key)
            if previous is not None and previous == msg.command:
                self._unsettled_controls.pop(key, None)
        pending = self._control.get(key)
        if pending is None or pending.result.done() or msg.is_test:
            return
        if bool(getattr(msg.command, "select", False)) != pending.select:
            return
        if msg.command != pending.expected_command:
            return
        unknown_cause = msg.cause in (
            iec104.CommandResCause.UNKNOWN_TYPE,
            iec104.CommandResCause.UNKNOWN_CAUSE,
            iec104.CommandResCause.UNKNOWN_ASDU_ADDRESS,
            iec104.CommandResCause.UNKNOWN_IO_ADDRESS,
        )
        if unknown_cause:
            pending.result.set_result(msg)
        elif msg.cause == iec104.CommandResCause.ACTIVATION_CONFIRMATION:
            if msg.is_negative_confirm or pending.phase == "act_con":
                pending.result.set_result(msg)
            else:
                pending.accepted = True
        elif (
            msg.cause == iec104.CommandResCause.ACTIVATION_TERMINATION
            and pending.phase == "act_term"
            and pending.accepted
        ):
            pending.result.set_result(msg)

    def _interrogation_reply(
        self, msg: iec104.InterrogationMsg | iec104.CounterInterrogationMsg
    ) -> None:
        ca = msg.asdu_address
        pending = self._interrogation.get(ca)
        if pending is None or msg.is_test:
            return
        if isinstance(msg, iec104.InterrogationMsg) != (pending.kind == "station"):
            return
        expected_request = (
            (20 if pending.group is None else 20 + pending.group)
            if pending.kind == "station"
            else (5 if pending.group is None else pending.group)
        )
        if msg.request != expected_request:
            return
        if isinstance(msg, iec104.CounterInterrogationMsg) and msg.freeze != iec104.FreezeCode.READ:
            return
        if msg.cause in (
            iec104.CommandResCause.UNKNOWN_TYPE,
            iec104.CommandResCause.UNKNOWN_CAUSE,
            iec104.CommandResCause.UNKNOWN_ASDU_ADDRESS,
            iec104.CommandResCause.UNKNOWN_IO_ADDRESS,
        ):
            if not pending.result.done():
                pending.result.set_exception(
                    ProtocolError(f"IEC104 interrogation rejected: {msg.cause}")
                )
        elif msg.cause == iec104.CommandResCause.ACTIVATION_CONFIRMATION:
            if msg.is_negative_confirm and not pending.result.done():
                pending.result.set_exception(ProtocolError("IEC104 interrogation rejected"))
            else:
                pending.accepted = True
        elif msg.cause == iec104.CommandResCause.ACTIVATION_TERMINATION and pending.accepted:
            if not pending.result.done() and msg.is_negative_confirm:
                pending.result.set_exception(
                    ProtocolError("IEC104 interrogation negatively terminated")
                )
            elif not pending.result.done():
                pending.result.set_result(None)
