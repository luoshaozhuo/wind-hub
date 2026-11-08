"""Bounded, best-effort control submission with per-target FIFO and no retries."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from time import monotonic

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc as rpc
from contracts import common_pb2 as common
from contracts.scalar_codec import encode_scalar
from industrial_gateway.application.variable_store import Scalar
from industrial_gateway.infrastructure.config.v1_schema import (
    ControlConfig,
    service_path,
)

logger = logging.getLogger(__name__)

# These statuses explicitly reject request execution at the RPC boundary.
# Transport and deadline failures can have executed and must remain UNKNOWN.
_AUTHORITATIVE_REJECTIONS = frozenset(
    {
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.OUT_OF_RANGE,
        grpc.StatusCode.UNIMPLEMENTED,
    }
)


def _definitive_rpc_rejection(error: grpc.RpcError) -> bool:
    """Recognize explicit RPC rejection without assuming transport settlement."""
    code = getattr(error, "code", None)
    if not callable(code):
        return False
    return code() in _AUTHORITATIVE_REJECTIONS


class ForwardState(StrEnum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    OVERLOADED = "OVERLOADED"


@dataclass(frozen=True, slots=True)
class ForwardRecord:
    event_id: str
    gateway_id: str
    device_id: str
    point_id: str
    value: Scalar
    revision: str
    captured_at: datetime
    state: ForwardState
    command_id: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ForwardCommand:
    event_id: str
    gateway_id: str
    device_id: str
    point_id: str
    value: Scalar
    revision: str
    captured_at: datetime
    expires_at: datetime


RecordObserver = Callable[[ForwardRecord], None]


class CommandForwarder:
    """Send each accepted event at most once; transport ambiguity is UNKNOWN."""

    def __init__(
        self,
        config: ControlConfig,
        *,
        observer: RecordObserver | None = None,
        project_root: Path,
    ) -> None:
        self._config = config
        self._project_root = project_root
        self._observer = observer
        self._queue: asyncio.Queue[ForwardCommand] = asyncio.Queue(
            maxsize=config.forward_queue_capacity
        )
        self._workers: list[asyncio.Task[None]] = []
        self._channel: grpc.aio.Channel | None = None
        self._stub: rpc.CommanderServiceAsyncStub | None = None
        self._records: dict[str, ForwardRecord] = {}
        self._finished_order: deque[str] = deque()
        self._pending_by_gateway: dict[str, int] = {}
        self._pending_total = 0
        self._target_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._revision_cache: dict[str, tuple[str, float]] = {}
        self._revision_locks: dict[str, asyncio.Lock] = {}
        self._closing = False

    def _record(self, record: ForwardRecord) -> None:
        """Keep all pending entries plus at most 4096 terminal records."""
        pending_states = {
            ForwardState.QUEUED,
            ForwardState.VALIDATING,
            ForwardState.SUBMITTING,
        }
        previous = self._records.get(record.event_id)
        was_pending = previous is not None and previous.state in pending_states
        is_pending = record.state in pending_states
        if was_pending != is_pending:
            delta = 1 if is_pending else -1
            self._pending_total += delta
            self._pending_by_gateway[record.gateway_id] = (
                self._pending_by_gateway.get(record.gateway_id, 0) + delta
            )
        self._records[record.event_id] = record
        if not is_pending and (previous is None or was_pending):
            self._finished_order.append(record.event_id)
            while len(self._finished_order) > 4096:
                self._records.pop(self._finished_order.popleft(), None)
        if self._observer is not None:
            self._observer(record)

    @property
    def records(self) -> tuple[ForwardRecord, ...]:
        return tuple(self._records.values())

    @property
    def pending(self) -> int:
        return self._pending_total

    def pending_for(self, gateway_id: str) -> int:
        """Return live per-Gateway pending count in constant time."""
        return self._pending_by_gateway.get(gateway_id, 0)

    def capture(
        self,
        *,
        event_id: str,
        gateway_id: str,
        device_id: str,
        point_id: str,
        value: Scalar,
        revision: str,
    ) -> bool:
        """Freeze target and revision at capture; never await on the protocol path."""
        event = ForwardCommand(
            event_id=event_id,
            gateway_id=gateway_id,
            device_id=device_id,
            point_id=point_id,
            value=value,
            revision=revision,
            captured_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(milliseconds=self._config.submit_deadline_ms),
        )
        state = ForwardState.QUEUED
        if self._closing or self._queue.full():
            state = ForwardState.OVERLOADED
        else:
            self._queue.put_nowait(event)
        self._record(
            ForwardRecord(
                event_id=event.event_id,
                gateway_id=gateway_id,
                device_id=device_id,
                point_id=point_id,
                value=value,
                revision=revision,
                captured_at=event.captured_at,
                state=state,
                error=("forward queue unavailable" if state is ForwardState.OVERLOADED else ""),
            )
        )
        return state is ForwardState.QUEUED

    async def start(self) -> None:
        """Start the bounded pool; the channel is shared by all submissions."""
        if self._workers:
            raise RuntimeError("forwarder already started")
        self._closing = False
        if self._config.tls.enabled:
            tls = self._config.tls
            if tls.ca_file is None or tls.cert_file is None or tls.key_file is None:
                raise ValueError("Commander mTLS requires CA, certificate and key")
            credentials = grpc.ssl_channel_credentials(
                root_certificates=service_path(self._project_root, tls.ca_file).read_bytes(),
                private_key=service_path(self._project_root, tls.key_file).read_bytes(),
                certificate_chain=service_path(self._project_root, tls.cert_file).read_bytes(),
            )
            channel = grpc.aio.secure_channel(self._config.commander_endpoint, credentials)
        else:
            channel = grpc.aio.insecure_channel(self._config.commander_endpoint)
        self._channel = channel
        # mypy-protobuf 的 __new__ 重载按 aio.Channel 直接解析为 AsyncStub；
        # 运行时只有一个 Stub 类，同步/异步通道共用。
        self._stub = rpc.CommanderServiceStub(channel)
        self._workers = [
            asyncio.create_task(self._work()) for _ in range(self._config.max_forward_inflight)
        ]

    async def _work(self) -> None:
        while True:
            event = await self._queue.get()
            key = (event.device_id, event.point_id)
            lock = self._target_locks.setdefault(key, asyncio.Lock())
            try:
                async with lock:
                    await self._submit(event)
            except asyncio.CancelledError:
                record = self._records[event.event_id]
                if record.state in {
                    ForwardState.QUEUED,
                    ForwardState.VALIDATING,
                }:
                    self._finish(record, ForwardState.REJECTED, "not submitted")
                elif record.state is ForwardState.SUBMITTING:
                    self._finish(record, ForwardState.UNKNOWN, "submission cancelled")
                raise
            except Exception as exc:
                logger.exception("Commander forwarding worker failed")
                record = self._records[event.event_id]
                state = (
                    ForwardState.UNKNOWN
                    if record.state is ForwardState.SUBMITTING
                    else ForwardState.REJECTED
                )
                self._finish(record, state, str(exc))
            finally:
                self._queue.task_done()

    async def _device_revision(
        self,
        device_id: str,
        expires_at: datetime,
    ) -> str:
        """Resolve Commander-owned revision with short-lived, serialized caching."""
        lock = self._revision_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            cached = self._revision_cache.get(device_id)
            now = monotonic()
            if cached is not None and cached[1] > now:
                return cached[0]
            if self._stub is None:
                raise RuntimeError("Commander channel not started")
            remaining = (expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TimeoutError("event expired before revision lookup")
            response = await self._stub.GetDevice(common.ObjectId(id=device_id), timeout=remaining)
            revision = response.device.revision
            if not revision:
                raise ValueError(f"Commander device {device_id} has no revision")
            self._revision_cache[device_id] = (
                revision,
                monotonic() + 1.0,
            )
            return revision

    async def _submit(self, event: ForwardCommand) -> None:
        original = self._records[event.event_id]
        self._record(
            ForwardRecord(
                event_id=event.event_id,
                gateway_id=event.gateway_id,
                device_id=event.device_id,
                point_id=event.point_id,
                value=event.value,
                revision=event.revision,
                captured_at=event.captured_at,
                state=ForwardState.VALIDATING,
            )
        )
        remaining = (event.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            self._finish(original, ForwardState.REJECTED, "queue entry expired")
            return
        if self._stub is None:
            self._finish(original, ForwardState.REJECTED, "Commander not started")
            return
        try:
            device_revision = await self._device_revision(event.device_id, event.expires_at)
        except asyncio.CancelledError:
            self._finish(original, ForwardState.REJECTED, "lookup cancelled")
            raise
        except (grpc.RpcError, RuntimeError, TimeoutError, ValueError) as exc:
            self._finish(
                original,
                ForwardState.REJECTED,
                f"device revision unavailable: {exc}",
            )
            return
        remaining = (event.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            self._finish(original, ForwardState.REJECTED, "lookup expired")
            return
        deadline = Timestamp()
        deadline.FromDatetime(event.expires_at)
        command = pb.SubmitCommandRequest(
            device_id=event.device_id,
            point_id=event.point_id,
            value=encode_scalar(event.value),
            expires_at=deadline,
            expected_revision=device_revision,
        )
        self._record(
            ForwardRecord(
                event_id=event.event_id,
                gateway_id=event.gateway_id,
                device_id=event.device_id,
                point_id=event.point_id,
                value=event.value,
                revision=event.revision,
                captured_at=event.captured_at,
                state=ForwardState.SUBMITTING,
            )
        )
        try:
            response = await self._stub.SubmitCommand(command, timeout=remaining)
        except asyncio.CancelledError:
            self._finish(original, ForwardState.UNKNOWN, "shutdown during submission")
            raise
        except grpc.RpcError as exc:
            if _definitive_rpc_rejection(exc):
                self._revision_cache.pop(event.device_id, None)
                self._finish(original, ForwardState.REJECTED, str(exc))
            else:
                self._finish(original, ForwardState.UNKNOWN, str(exc))
        except (OSError, RuntimeError, ValueError) as exc:
            self._finish(original, ForwardState.UNKNOWN, str(exc))
        else:
            if response.accepted and not response.command_id:
                self._finish(
                    original,
                    ForwardState.UNKNOWN,
                    "accepted without authoritative command_id",
                )
            else:
                state = ForwardState.ACCEPTED if response.accepted else ForwardState.REJECTED
                if not response.accepted:
                    self._revision_cache.pop(event.device_id, None)
                self._finish(original, state, response.message, response.command_id)

    def _finish(
        self,
        record: ForwardRecord,
        state: ForwardState,
        error: str = "",
        command_id: str = "",
    ) -> None:
        self._record(
            ForwardRecord(
                event_id=record.event_id,
                gateway_id=record.gateway_id,
                device_id=record.device_id,
                point_id=record.point_id,
                value=record.value,
                revision=record.revision,
                captured_at=record.captured_at,
                state=state,
                command_id=command_id,
                error=error,
            )
        )

    async def get_command_result(self, command_id: str) -> pb.GetCommandResultResponse:
        """Query authoritative Commander status without resubmitting commands."""
        if not command_id.strip():
            raise ValueError("command_id is required")
        if self._stub is None:
            raise RuntimeError("Commander channel not started")
        response = await self._stub.GetCommandResult(
            pb.GetCommandResultRequest(command_id=command_id),
            timeout=self._config.submit_deadline_ms / 1000,
        )
        return response

    async def stop(self) -> None:
        """Discard queued events and mark in-flight submissions UNKNOWN."""
        self._closing = True
        workers, self._workers = self._workers, []
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        while not self._queue.empty():
            event = self._queue.get_nowait()
            self._queue.task_done()
            self._finish(
                self._records[event.event_id],
                ForwardState.REJECTED,
                "shutdown before submission",
            )
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._stub = None
