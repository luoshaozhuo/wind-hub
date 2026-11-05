"""Bounded, request-correlated local control protocol for Sink worker processes.

The protocol carries management commands and bounded evidence only. Sample batches
are transported directly over NATS/JetStream and never via this IPC channel.
"""

from __future__ import annotations

import asyncio
import json
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

MAX_IPC_FRAME_BYTES = 65_536

ControlOperation = Literal[
    "APPLY",
    "START",
    "STOP",
    "GET_STATUS",
    "GET_METRICS",
    "DIAGNOSE",
    "SHUTDOWN",
]

_ALLOWED_OPERATIONS = frozenset(
    {"APPLY", "START", "STOP", "GET_STATUS", "GET_METRICS", "DIAGNOSE", "SHUTDOWN"}
)
_HEADER = struct.Struct("!I")


class IpcProtocolError(ValueError):
    """Malformed or uncorrelated worker control message."""


@dataclass(frozen=True, slots=True)
class ControlRequest:
    request_id: str
    operation: ControlOperation
    generation: str
    delivery_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id or not self.generation:
            raise IpcProtocolError("IPC request_id and generation must not be empty")
        if self.operation not in _ALLOWED_OPERATIONS:
            raise IpcProtocolError("unsupported IPC operation")
        if self.operation in {"START", "STOP"} and not self.delivery_id:
            raise IpcProtocolError("delivery_id is required for START/STOP")

    def to_wire(self) -> dict[str, str]:
        return {
            "kind": "request",
            "request_id": self.request_id,
            "operation": self.operation,
            "generation": self.generation,
            "delivery_id": self.delivery_id,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> ControlRequest:
        expected = {"kind", "request_id", "operation", "generation", "delivery_id"}
        if set(raw) != expected or raw["kind"] != "request":
            raise IpcProtocolError("invalid request fields")
        if any(not isinstance(raw[key], str) for key in expected - {"kind"}):
            raise IpcProtocolError("request fields must be strings")
        if raw["operation"] not in _ALLOWED_OPERATIONS:
            raise IpcProtocolError("invalid IPC operation")
        return cls(
            request_id=raw["request_id"],
            operation=cast(ControlOperation, raw["operation"]),
            generation=raw["generation"],
            delivery_id=raw["delivery_id"],
        )


@dataclass(frozen=True, slots=True)
class ControlReply:
    request_id: str
    generation: str
    status: Literal["APPLIED", "PENDING", "FAILED", "UNKNOWN"]
    evidence: dict[str, str]

    def __post_init__(self) -> None:
        if not self.request_id or not self.generation:
            raise IpcProtocolError("IPC reply identity is required")
        if self.status not in {"APPLIED", "PENDING", "FAILED", "UNKNOWN"}:
            raise IpcProtocolError("unsupported IPC reply status")
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in self.evidence.items()):
            raise IpcProtocolError("IPC evidence must be string to string")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "reply",
            "request_id": self.request_id,
            "generation": self.generation,
            "status": self.status,
            "evidence": self.evidence,
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> ControlReply:
        if set(raw) != {"kind", "request_id", "generation", "status", "evidence"}:
            raise IpcProtocolError("invalid reply fields")
        if raw["kind"] != "reply" or any(
            not isinstance(raw[key], str)
            for key in ("request_id", "generation", "status")
        ):
            raise IpcProtocolError("invalid reply identity")
        if not isinstance(raw["evidence"], dict):
            raise IpcProtocolError("invalid reply evidence")
        return cls(
            request_id=raw["request_id"],
            generation=raw["generation"],
            status=cast(Literal["APPLIED", "PENDING", "FAILED", "UNKNOWN"], raw["status"]),
            evidence=raw["evidence"],
        )


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one strict, size-limited control message."""
    size = _HEADER.unpack(await reader.readexactly(_HEADER.size))[0]
    if not 0 < size <= MAX_IPC_FRAME_BYTES:
        raise IpcProtocolError("invalid IPC frame length")
    raw = await reader.readexactly(size)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcProtocolError("invalid IPC JSON frame") from exc
    if not isinstance(payload, dict):
        raise IpcProtocolError("IPC frame must be an object")
    return payload


async def write_frame(
    writer: asyncio.StreamWriter, payload: dict[str, Any]
) -> None:
    """Write only bounded management messages; no arbitrary sample payload."""
    if "samples" in payload:
        raise IpcProtocolError("sample data must not be sent through IPC")
    data = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if not 0 < len(data) <= MAX_IPC_FRAME_BYTES:
        raise IpcProtocolError("IPC frame exceeds maximum length")
    writer.write(_HEADER.pack(len(data)) + data)
    await writer.drain()


class WorkerControlClient:
    """Single outstanding mutation per worker connection, with correlation."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self._broken = False

    @property
    def broken(self) -> bool:
        return self._broken

    async def close(self) -> None:
        self._broken = True
        self._writer.close()
        await self._writer.wait_closed()

    async def request(
        self,
        operation: ControlOperation,
        generation: str,
        *,
        delivery_id: str = "",
        timeout_seconds: float = 5.0,
    ) -> ControlReply:
        if timeout_seconds <= 0:
            raise ValueError("IPC timeout must be positive")
        if self._broken:
            raise ConnectionError("IPC connection cannot be reused after failed correlation")
        request = ControlRequest(
            request_id=uuid.uuid4().hex,
            operation=operation,
            generation=generation,
            delivery_id=delivery_id,
        )
        async with self._lock:
            if self._broken:
                raise ConnectionError("IPC request channel is unavailable")
            try:
                await asyncio.wait_for(
                    write_frame(self._writer, request.to_wire()),
                    timeout=timeout_seconds,
                )
                raw = await asyncio.wait_for(
                    read_frame(self._reader), timeout=timeout_seconds
                )
                reply = ControlReply.from_wire(raw)
                if (
                    reply.request_id != request.request_id
                    or reply.generation != generation
                ):
                    raise IpcProtocolError("IPC reply correlation mismatch")
                return reply
            except (TimeoutError, OSError, asyncio.IncompleteReadError, IpcProtocolError):
                # The remote command may already have executed. Never consume
                # an old reply as if it belonged to a later request.
                self._broken = True
                self._writer.close()
                raise
