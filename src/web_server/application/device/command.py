"""设备写控制与真实回读服务。"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

from core.application.protocol_contract import WriteOutcome
from web_server.application.device.collector_read import DeviceReadPort
from web_server.application.device.command_model import Command
from web_server.application.device.durable_command import DurableCommandRegistry
from web_server.application.port.point_store import TrendStore
from web_server.application.port.worker import CommanderPort


class CommandIdConflictError(ValueError):
    """同一 command_id 携带不同写入内容——拒绝执行，防止语义混淆。"""


class DeviceCommandResult(BaseModel):
    """设备写入确认 + 回读结果。"""

    command_id: str
    requested: Any
    outcome: WriteOutcome
    success: bool
    error: str | None = None
    sent_at: datetime
    finished_at: datetime
    latency_ms: float
    readback: Any = None
    readback_timestamp: datetime | None = None
    readback_quality: str | None = None
    readback_error: str | None = None


class DeviceCommandService:
    """将客户端命令 ID 持久化为 at-most-once 控制防重边界。"""

    def __init__(
        self,
        commander: CommanderPort,
        reads: DeviceReadPort,
        trend: TrendStore,
        journal: DurableCommandRegistry,
    ) -> None:
        self._commander = commander
        self._reads = reads
        self._trend = trend
        self._journal = journal
        self._lock = asyncio.Lock()
        self._in_flight: dict[str, tuple[str, asyncio.Task[DeviceCommandResult]]] = {}

    @staticmethod
    def _fingerprint(device_id: str, point_id: str, value: Any, timeout: float) -> str:
        if not isinstance(value, bool | int | float | str):
            raise TypeError("control value must be a writable scalar")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("control value must be finite")
        payload = json.dumps(
            [device_id, point_id, value, timeout],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _unresolved(command_id: str, requested: Any) -> DeviceCommandResult:
        """持久化预留存在但没有终态时，不得自动重发。"""
        now = datetime.now(UTC)
        return DeviceCommandResult(
            command_id=command_id,
            requested=requested,
            outcome=WriteOutcome.UNKNOWN,
            success=False,
            error="previous control outcome is unresolved; command was not retransmitted",
            sent_at=now,
            finished_at=now,
            latency_ms=0.0,
        )

    async def send(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float = 5.0,
        command_id: str | None = None,
    ) -> DeviceCommandResult:
        """同一 command_id 只允许一次首次提交；跨重启安全失效闭锁。"""
        identifier = command_id or str(uuid.uuid4())
        if not identifier.strip() or len(identifier) > 128:
            raise ValueError("command_id must contain 1..128 non-whitespace characters")
        fingerprint = self._fingerprint(device_id, point_id, value, timeout)
        async with self._lock:
            pending = self._in_flight.get(identifier)
            if pending is not None:
                previous_fingerprint, task = pending
                if previous_fingerprint != fingerprint:
                    raise CommandIdConflictError(
                        f"command_id {identifier!r} reused for different command"
                    )
            else:
                try:
                    state, previous = await self._journal.reserve(identifier, fingerprint)
                except ValueError as exc:
                    raise CommandIdConflictError(str(exc)) from exc
                if state == "completed":
                    if previous is None:
                        raise RuntimeError("completed command is missing persisted result")
                    return DeviceCommandResult.model_validate_json(previous)
                if state == "pending":
                    return self._unresolved(identifier, value)
                task = asyncio.create_task(
                    self._execute_and_record(
                        device_id,
                        point_id,
                        value,
                        timeout=timeout,
                        command_id=identifier,
                        fingerprint=fingerprint,
                    )
                )
                self._in_flight[identifier] = (fingerprint, task)
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                existing = self._in_flight.get(identifier)
                if existing is not None and existing[1] is task:
                    self._in_flight.pop(identifier, None)

    async def _execute_and_record(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float,
        command_id: str,
        fingerprint: str,
    ) -> DeviceCommandResult:
        try:
            result = await self._execute(
                device_id, point_id, value, timeout=timeout, command_id=command_id
            )
        except asyncio.CancelledError:
            # 预留记录仍为 pending；再次请求只会得到 UNKNOWN。
            raise
        except Exception as exc:
            result = self._unresolved(command_id, value)
            result.error = f"control submission/result is uncertain: {exc}"
        await self._journal.finish(command_id, fingerprint, result.model_dump_json())
        return result

    async def _execute(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float,
        command_id: str | None,
    ) -> DeviceCommandResult:
        """执行单次写入与回读闭环（不含幂等去重）。"""
        command = Command(
            command_id=command_id or str(uuid.uuid4()),
            device_id=device_id,
            point_id=point_id,
            value=value,
            timeout=timeout,
        )
        started = time.monotonic()
        result = await self._commander.write(command)
        latency_ms = (time.monotonic() - started) * 1000
        readback = None
        readback_timestamp = None
        readback_quality = None
        readback_error = None

        if result.outcome is WriteOutcome.CONFIRMED_SUCCESS:
            try:
                observed = await self._reads.read_point(device_id, point_id)
            except Exception as exc:
                readback_error = str(exc) or type(exc).__name__
            else:
                readback = observed.value
                readback_timestamp = observed.timestamp
                readback_quality = observed.quality.value
                self._trend.append_batch([observed])

        return DeviceCommandResult(
            command_id=result.command_id,
            requested=value,
            outcome=result.outcome,
            success=result.outcome is WriteOutcome.CONFIRMED_SUCCESS,
            error=result.error,
            sent_at=command.issued_at,
            finished_at=result.finished_at,
            latency_ms=latency_ms,
            readback=readback,
            readback_timestamp=readback_timestamp,
            readback_quality=readback_quality,
            readback_error=readback_error,
        )
