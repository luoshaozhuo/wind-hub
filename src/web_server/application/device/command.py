"""设备写控制与真实回读服务。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Hashable
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from core.application.command import Command
from core.application.protocol_contract import WriteOutcome
from web_server.application.port.point_store import TrendStore
from web_server.application.port.worker import CommanderPort


def _freeze(value: Any) -> Hashable:
    """把写入值规范为可哈希形式，供幂等键内容比对。"""
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, Hashable):
        return value
    return repr(value)


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
    """通过独立 Commander 完成设备写入与回读闭环。"""

    #: 已完成 command_id 结果缓存上限——幂等窗口，超出后最旧记录淘汰。
    _COMPLETED_CAPACITY = 256

    def __init__(
        self,
        commander: CommanderPort,
        trend: TrendStore,
    ) -> None:
        self._commander = commander
        self._trend = trend
        self._in_flight: dict[str, tuple[Hashable, asyncio.Task[DeviceCommandResult]]] = {}
        self._completed: OrderedDict[str, tuple[Hashable, DeviceCommandResult]] = OrderedDict()

    async def send(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float = 5.0,
        command_id: str | None = None,
    ) -> DeviceCommandResult:
        """写入设备；成功后经 Commander 即时回读，并追加短期趋势样本。

        幂等语义：显式 ``command_id`` 是幂等键。相同 command_id + 相同写入
        内容的并发请求共享同一次执行（不发生二次写入）；已完成请求直接返回
        首次结果；相同 command_id + 不同内容抛 ``CommandIdConflictError``。
        结果未知（UNKNOWN）时同样缓存——禁止借重复请求变相重发。
        """
        key: Hashable = (device_id, point_id, _freeze(value), timeout)
        if command_id is not None:
            cached = self._completed.get(command_id)
            if cached is not None:
                cached_key, cached_result = cached
                if cached_key != key:
                    raise CommandIdConflictError(
                        f"command_id '{command_id}' was used with different command content"
                    )
                return cached_result
            pending = self._in_flight.get(command_id)
            if pending is not None:
                pending_key, task = pending
                if pending_key != key:
                    raise CommandIdConflictError(
                        f"command_id '{command_id}' was used with different command content"
                    )
                return await asyncio.shield(task)

            task = asyncio.create_task(
                self._execute(device_id, point_id, value, timeout=timeout, command_id=command_id)
            )
            self._in_flight[command_id] = (key, task)
            try:
                result = await asyncio.shield(task)
            finally:
                self._in_flight.pop(command_id, None)
            self._completed[command_id] = (key, result)
            while len(self._completed) > self._COMPLETED_CAPACITY:
                self._completed.popitem(last=False)
            return result

        return await self._execute(device_id, point_id, value, timeout=timeout, command_id=None)

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
                observed = await self._commander.read_point(device_id, point_id)
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
