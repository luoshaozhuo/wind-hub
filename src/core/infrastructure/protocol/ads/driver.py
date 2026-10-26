"""单 PLC ADS 协议驱动：连接策略、读写、订阅与安全关闭。"""

from __future__ import annotations

import asyncio
import struct
import sys
from collections.abc import Sequence
from ctypes import Structure, c_uint32
from datetime import UTC, datetime
from typing import Any

import pyads  # type: ignore[import-untyped]
from pyads.constants import ADSIGRP_SUMUP_READ, ADSIGRP_SUMUP_WRITE  # type: ignore[import-untyped]

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.port.protocol import (
    ListenHandle,
    ProtocolSampleCallback,
)
from core.application.protocol_contract import (
    ConnectionHealth,
    InterrogationRequest,
    ProtocolCapability,
    ProtocolLifecycleState,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
)
from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .codec import (
    _coerce_write_value,
    _decode_sum_write_errors,
    _encode_value,
    compile_decoder,
    encode_sum_write,
)
from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point, symbol_index_address
from .session import ADSCommandNotSentError, _AdsSession
from .subscription import ADSSubscription


class _SumReadItem(Structure):
    _fields_ = [("iGroup", c_uint32), ("iOffset", c_uint32), ("size", c_uint32)]


_LOCAL_IDENTITY: ADSLocalConfig | None = None
_LOCAL_PORT_OPEN = False
_LOCAL_CLOSED = False
_LOCAL_LOCK = asyncio.Lock()
_REMOTE_ROUTE_LOCK = asyncio.Lock()
_ROUTE_TASKS: set[asyncio.Task[bool]] = set()
_TARGET_OWNERS: dict[tuple[str, str], ADSDriver] = {}


class ADSDriver:
    """仅有一个活动 Session 的 ADS ProtocolPort 实现。"""

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        self._config: ADSConfig = parse_ads_config(endpoint, protocol_options)
        if point_table.protocol.name != "ads":
            raise ConfigError("ADS driver requires an ADS point table")
        if not self._config.target_net_id:
            raise ConfigError("ADS target_net_id is required")
        self._identity = (self._config.target_net_id, self._config.host)
        self._state = ProtocolLifecycleState.DISCONNECTED
        self._lifecycle = asyncio.Lock()
        self._session: _AdsSession | None = None
        self._subscriptions: set[ADSSubscription] = set()
        self._connect_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._connect_expired = False
        self._points: dict[str, ADSPoint] = {}
        self.update_point_table(point_table)

    @classmethod
    async def initialize_local(cls, config: ADSLocalConfig) -> None:
        global _LOCAL_IDENTITY, _LOCAL_PORT_OPEN, _LOCAL_CLOSED
        async with _LOCAL_LOCK:
            if _LOCAL_IDENTITY is not None:
                if config != _LOCAL_IDENTITY:
                    raise ConfigError("ADS local AMS identity mismatch")
                return
            if sys.platform.startswith("linux"):
                await asyncio.to_thread(pyads.open_port)
                _LOCAL_PORT_OPEN = True
                try:
                    await asyncio.to_thread(pyads.set_local_address, config.local_ams_net_id)
                except BaseException:
                    await asyncio.to_thread(pyads.close_port)
                    _LOCAL_PORT_OPEN = False
                    raise
            _LOCAL_IDENTITY = config
            _LOCAL_CLOSED = False

    @classmethod
    async def close_local(cls) -> None:
        global _LOCAL_IDENTITY, _LOCAL_PORT_OPEN, _LOCAL_CLOSED
        async with _LOCAL_LOCK:
            if _TARGET_OWNERS or _ROUTE_TASKS:
                raise ProtocolError("ADS sessions or route operations still own AMS resources")
            if _LOCAL_PORT_OPEN:
                await asyncio.to_thread(pyads.close_port)
                _LOCAL_PORT_OPEN = False
            _LOCAL_IDENTITY = None
            _LOCAL_CLOSED = True

    @property
    def state(self) -> ProtocolLifecycleState:
        return self._state

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.READ_MANY,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
                ProtocolCapability.SUBSCRIBE,
            }
        )

    def update_point_table(self, point_table: PointTable) -> None:
        if point_table.protocol.name != "ads":
            raise ConfigError("ADS point table protocol mismatch")
        mapped = {point.point_id: parse_ads_point(point) for point in point_table.points.values()}
        if self.is_open() and any(item.symbol for item in mapped.values()):
            raise ConfigError("symbol mapping changes require reconnect")
        self._points = mapped

    def _new_session(self) -> _AdsSession:
        def factory() -> pyads.Connection:
            connection = pyads.Connection(
                self._config.target_net_id,
                self._config.target_port,
                self._config.host,
            )
            return connection

        return _AdsSession(factory)

    async def connect(self) -> None:
        if self._state is ProtocolLifecycleState.CONNECTED:
            return
        if self._state in {ProtocolLifecycleState.FAULTED, ProtocolLifecycleState.CLOSING}:
            raise ProtocolError("ADS connection is not safely available")
        if self._connect_task is None or self._connect_task.done():
            self._connect_expired = False
            if self._state is ProtocolLifecycleState.DISCONNECTED:
                self._close_task = None
            task = asyncio.create_task(self._connect_once())
            self._connect_task = task
        budget_ms = _LOCAL_IDENTITY.connect_timeout_ms if _LOCAL_IDENTITY else 3000
        try:
            await asyncio.wait_for(asyncio.shield(self._connect_task), timeout=budget_ms / 1000)
        except TimeoutError:
            self._connect_expired = True
            self._schedule_late_connect_cleanup()
            raise ProtocolError("ADS connection deadline exceeded; cleanup is pending") from None
        except asyncio.CancelledError:
            # A cancelled waiter must not poison a shared connection attempt.
            raise

    def _schedule_late_connect_cleanup(self) -> None:
        """超时的建链任务结束后，仍须关闭它可能建立的连接。"""
        task = self._connect_task
        if task is None:
            return

        def cleanup(completed: asyncio.Task[None]) -> None:
            if not completed.cancelled():
                completed.exception()  # consume a late failed connection attempt
            if self._state is ProtocolLifecycleState.CONNECTED:
                asyncio.create_task(self.close())

        task.add_done_callback(cleanup)

    async def _connect_once(self) -> None:
        async with self._lifecycle:
            if self._state is ProtocolLifecycleState.CONNECTED:
                return
            if self._state is not ProtocolLifecycleState.DISCONNECTED or _LOCAL_CLOSED:
                raise ProtocolError("ADS driver is not available for connect")
            owner = _TARGET_OWNERS.get(self._identity)
            if owner is not None and owner is not self:
                raise ProtocolError("ADS target AMS identity is already in use")
            _TARGET_OWNERS[self._identity] = self
            self._state = ProtocolLifecycleState.CONNECTING
            try:
                for attempt in range(2 if self._config.route_auto_add else 1):
                    session = self._new_session()
                    self._session = session
                    try:
                        await session.open()
                        request_ms = (
                            _LOCAL_IDENTITY.request_timeout_ms
                            if _LOCAL_IDENTITY is not None
                            else int(self._config.timeout * 1000)
                        )
                        await session.execute("set_timeout", request_ms)
                        if self._connect_expired:
                            raise ProtocolError("ADS connection deadline expired")
                        result = await session.execute("read_state")
                        if self._connect_expired or not result or result[0] != pyads.ADSSTATE_RUN:
                            raise ProtocolError("ADS PLC is not in RUN state")
                        self._points = await self._resolve_points(session)
                        self._state = ProtocolLifecycleState.CONNECTED
                        return
                    except Exception:
                        try:
                            await session.close()
                        except Exception:
                            self._state = ProtocolLifecycleState.FAULTED
                            raise
                        self._session = None
                        if (
                            attempt == 0
                            and session.open_failed
                            and self._config.route_auto_add
                            and not self._connect_expired
                        ):
                            if _LOCAL_IDENTITY is None:
                                raise ConfigError(
                                    "ADS local AMS identity is not initialized"
                                ) from None
                            async with _REMOTE_ROUTE_LOCK:
                                route_task = asyncio.create_task(
                                    asyncio.to_thread(
                                        pyads.add_route_to_plc,
                                        _LOCAL_IDENTITY.local_ams_net_id,
                                        _LOCAL_IDENTITY.local_ip,
                                        self._config.host,
                                        self._config.username,
                                        self._config.password,
                                    )
                                )
                                _ROUTE_TASKS.add(route_task)
                                def release_route(done: asyncio.Task[bool]) -> None:
                                    _ROUTE_TASKS.discard(done)
                                    if not done.cancelled():
                                        done.exception()

                                route_task.add_done_callback(release_route)
                                success = await asyncio.shield(route_task)
                            if success and not self._connect_expired:
                                continue
                        raise
            except BaseException:
                if self._state is not ProtocolLifecycleState.FAULTED:
                    if self._session is not None:
                        self._state = ProtocolLifecycleState.FAULTED
                    else:
                        self._state = ProtocolLifecycleState.DISCONNECTED
                        _TARGET_OWNERS.pop(self._identity, None)
                raise

    async def _resolve_points(self, session: _AdsSession) -> dict[str, ADSPoint]:
        """Resolve and validate every configured symbol against this PLC session."""
        resolved: dict[str, ADSPoint] = {}
        for point_id, point in self._points.items():
            try:
                info = await session.execute("get_symbol", point.symbol)
                group, offset = symbol_index_address(point.symbol, info)
            except Exception as exc:
                raise ConfigError(
                    f"ADS point '{point_id}': cannot resolve symbol '{point.symbol}': {exc}"
                ) from exc

            if point.address_resolved and (point.group, point.offset) != (group, offset):
                raise ConfigError(
                    f"ADS point '{point_id}': configured group/offset "
                    f"({point.group}, {point.offset}) does not match symbol "
                    f"'{point.symbol}' ({group}, {offset})"
                )
            if self._connect_expired:
                raise ProtocolError("ADS connection deadline expired")
            resolved[point_id] = point.resolved(index_group=group, index_offset=offset)
        return resolved

    async def close(self) -> None:
        if self._close_task is None:
            if self._state is ProtocolLifecycleState.CONNECTED:
                self._state = ProtocolLifecycleState.CLOSING
            self._close_task = asyncio.create_task(self._close_once())
        budget_ms = _LOCAL_IDENTITY.close_timeout_ms if _LOCAL_IDENTITY else 3000
        try:
            await asyncio.wait_for(asyncio.shield(self._close_task), timeout=budget_ms / 1000)
        except TimeoutError:
            # _close_once alone owns final lifecycle transitions.
            raise ProtocolError("ADS close deadline exceeded; physical cleanup continues") from None

    async def _close_once(self) -> None:
        async with self._lifecycle:
            if self._state is ProtocolLifecycleState.DISCONNECTED:
                return
            self._state = ProtocolLifecycleState.CLOSING
            try:
                subscription_errors: list[Exception] = []
                for sub in tuple(self._subscriptions):
                    try:
                        await sub.stop()
                    except Exception as exc:
                        subscription_errors.append(exc)
                if self._session is not None:
                    await self._session.close()
                    if not self._session.is_settled:
                        raise ProtocolError("ADS session close is not settled")
                self._session = None
                if subscription_errors:
                    raise ExceptionGroup(
                        "ADS subscriptions not fully released", subscription_errors
                    )
                self._subscriptions.clear()
                self._state = ProtocolLifecycleState.DISCONNECTED
                _TARGET_OWNERS.pop(self._identity, None)
            except BaseException:
                self._state = ProtocolLifecycleState.FAULTED
                raise

    def is_open(self) -> bool:
        return (
            self._state is ProtocolLifecycleState.CONNECTED
            and self._session is not None
            and self._session.is_open
        )

    async def health(self) -> ConnectionHealth:
        if not self.is_open():
            return ConnectionHealth(False, self._state.value)
        try:
            session = self._require_session()
            plc, _ = await session.execute("read_state")
            return ConnectionHealth(plc == pyads.ADSSTATE_RUN, str(plc))
        except Exception as exc:
            return ConnectionHealth(False, str(exc))

    def _require_session(self) -> _AdsSession:
        if not self.is_open() or self._session is None:
            raise ProtocolError("ADS driver is not connected")
        return self._session

    def _mapped(self, ids: Sequence[str]) -> tuple[ADSPoint, ...]:
        try:
            return tuple(self._points[point_id] for point_id in ids)
        except KeyError as exc:
            raise ProtocolError(f"Unknown ADS point: {exc}") from exc

    async def read_one(self, point_id: str) -> ProtocolSample:
        session = self._require_session()
        point = self._mapped((point_id,))[0]
        raw = await session.execute(
            "read",
            point.group,
            point.offset,
            pyads.PLCTYPE_BYTE * point.size,
        )
        received_at = datetime.now(UTC)
        try:
            value = compile_decoder(point)(bytes(raw), 0)
            quality = Quality.GOOD
        except (ValueError, TypeError, UnicodeDecodeError, struct.error):
            value = None
            quality = Quality.BAD
        return ProtocolSample(
            point_id=point_id,
            value=value,
            quality=quality,
            timestamp=received_at,
            receive_time=received_at,
        )

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        session = self._require_session()
        mapped = self._mapped(point_ids)
        values: list[tuple[Any, Quality]] = []
        limit = self._config.max_subs_per_sum
        for start in range(0, len(mapped), limit):
            chunk = mapped[start : start + limit]
            request = (_SumReadItem * len(chunk))(
                *(_SumReadItem(p.group, p.offset, p.size) for p in chunk)
            )
            reply = await session.execute(
                "read_write",
                ADSIGRP_SUMUP_READ,
                len(chunk),
                None,
                request,
                None,
                check_length=False,
            )
            raw = bytes(reply)
            expected = 4 * len(chunk) + sum(p.size for p in chunk)
            if len(raw) < expected:
                raise ProtocolError("ADS Sum Read reply is shorter than expected")
            errors = struct.unpack_from(f"<{len(chunk)}I", raw)
            offset = 4 * len(chunk)
            for point, error in zip(chunk, errors, strict=True):
                if error:
                    values.append((None, Quality.BAD))
                else:
                    try:
                        values.append((compile_decoder(point)(raw, offset), Quality.GOOD))
                    except (ValueError, TypeError, UnicodeDecodeError, struct.error):
                        values.append((None, Quality.BAD))
                offset += point.size
        received_at = datetime.now(UTC)
        return tuple(
            ProtocolSample(
                point_id=point_id,
                value=value,
                quality=quality,
                timestamp=received_at,
                receive_time=received_at,
            )
            for point_id, (value, quality) in zip(point_ids, values, strict=True)
        )

    async def write_one(self, write: ProtocolWrite[Any]) -> ProtocolWriteResult:
        session = self._require_session()
        point = self._mapped((write.point,))[0]
        try:
            value = _coerce_write_value(write.value, point.data_type)
            raw = _encode_value(value, point)
        except (TypeError, ValueError, ConfigError, OverflowError) as exc:
            return ProtocolWriteResult(
                write.point, WriteOutcome.CONFIRMED_FAILURE, str(exc)
            )
        try:
            await session.execute(
                "write",
                point.group,
                point.offset,
                (pyads.PLCTYPE_BYTE * len(raw)).from_buffer_copy(raw),
                pyads.PLCTYPE_BYTE * len(raw),
            )
        except ADSCommandNotSentError as exc:
            return ProtocolWriteResult(
                write.point, WriteOutcome.CONFIRMED_FAILURE, str(exc)
            )
        except Exception as exc:
            return ProtocolWriteResult(write.point, WriteOutcome.UNKNOWN, str(exc))
        return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)

    async def write_many(
        self, writes: Sequence[ProtocolWrite[Any]]
    ) -> tuple[ProtocolWriteResult, ...]:
        if not writes:
            return ()
        session = self._require_session()
        mapped = self._mapped([item.point for item in writes])
        results: list[ProtocolWriteResult | None] = [None] * len(writes)
        prepared: list[tuple[int, ADSPoint, bytes]] = []
        for index, (write, point) in enumerate(zip(writes, mapped, strict=True)):
            try:
                value = _coerce_write_value(write.value, point.data_type)
                prepared.append((index, point, _encode_value(value, point)))
            except (TypeError, ValueError, ConfigError, OverflowError) as exc:
                results[index] = ProtocolWriteResult(
                    write.point, WriteOutcome.CONFIRMED_FAILURE, str(exc)
                )
        limit = self._config.max_subs_per_sum
        for start in range(0, len(prepared), limit):
            chunk = prepared[start : start + limit]
            try:
                reply = await session.execute(
                    "read_write",
                    ADSIGRP_SUMUP_WRITE,
                    len(chunk),
                    None,
                    encode_sum_write(chunk),
                    None,
                    check_length=False,
                )
                errors = _decode_sum_write_errors(reply, len(chunk))
            except Exception as exc:
                outcome = (
                    WriteOutcome.CONFIRMED_FAILURE
                    if isinstance(exc, ADSCommandNotSentError)
                    else WriteOutcome.UNKNOWN
                )
                for index, point, _ in chunk:
                    results[index] = ProtocolWriteResult(point.point_id, outcome, str(exc))
                for index, point, _ in prepared[start + len(chunk) :]:
                    results[index] = ProtocolWriteResult(
                        point.point_id, WriteOutcome.CONFIRMED_FAILURE, "not sent"
                    )
                break
            for (index, point, _), error in zip(chunk, errors, strict=True):
                results[index] = ProtocolWriteResult(
                    point.point_id,
                    (
                        WriteOutcome.CONFIRMED_SUCCESS
                        if error == 0
                        else WriteOutcome.CONFIRMED_FAILURE
                    ),
                    None if error == 0 else f"ADS error {error}",
                )
        if any(result is None for result in results):
            raise ProtocolError("ADS write result incomplete")
        return tuple(result for result in results if result is not None)

    def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> ADSSubscription:
        session = self._require_session()
        mapped = self._mapped(point_ids or tuple(self._points))
        subscription = ADSSubscription(
            session,
            mapped,
            callback,
            interval=0.1 if interval is None else interval,
            max_block_bytes=self._config.max_block_bytes,
            max_delay=self._config.max_delay,
            on_stopped=self._subscriptions.discard,
        )
        self._subscriptions.add(subscription)
        return subscription

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        del point_ids, callback, interval
        raise ProtocolCapabilityError("ADS does not support local listen")

    async def interrogate(self, request: InterrogationRequest) -> tuple[ProtocolSample, ...]:
        del request
        raise ProtocolCapabilityError("ADS does not support interrogation")
