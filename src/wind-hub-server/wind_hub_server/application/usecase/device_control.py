"""设备写控制与真实回读用例。"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from wind_hub_server.application.port.point_store import LatestPointStore, TrendStore
from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub_core.model.command import Command


class DeviceCommandResult(BaseModel):
    """设备写入确认 + 回读结果。"""

    command_id: str
    requested: Any
    success: bool
    error: str | None = None
    sent_at: datetime
    finished_at: datetime
    latency_ms: float
    readback: Any = None
    readback_timestamp: datetime | None = None
    readback_quality: str | None = None
    readback_error: str | None = None


class DeviceControlUseCase:
    """CommandDispatcher 上层的设备控制业务闭环。"""

    def __init__(
        self,
        command: CommandUseCase,
        query: QueryUseCase,
        latest: LatestPointStore,
        trend: TrendStore,
    ) -> None:
        self._command = command
        self._query = query
        self._latest = latest
        self._trend = trend

    async def send(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float = 5.0,
        command_id: str | None = None,
    ) -> DeviceCommandResult:
        """写入设备；写成功后直接回读同一点并刷新 Data/Trend 缓存。"""
        command = Command(
            command_id=command_id or str(uuid.uuid4()),
            device_id=device_id,
            point_id=point_id,
            value=value,
            timeout=timeout,
        )
        started = time.monotonic()
        result = await self._command.send(command)
        latency_ms = (time.monotonic() - started) * 1000
        readback = None
        readback_timestamp = None
        readback_quality = None
        readback_error = None
        if result.success:
            try:
                observed = await self._query.read_point(device_id, point_id)
            except Exception as exc:
                readback_error = str(exc) or type(exc).__name__
            else:
                readback = observed.value
                readback_timestamp = observed.timestamp
                readback_quality = observed.quality.value
                self._latest.put_batch([observed])
                self._trend.append_batch([observed])
        return DeviceCommandResult(
            command_id=result.command_id,
            requested=value,
            success=result.success,
            error=result.error,
            sent_at=command.issued_at,
            finished_at=result.finished_at,
            latency_ms=latency_ms,
            readback=readback,
            readback_timestamp=readback_timestamp,
            readback_quality=readback_quality,
            readback_error=readback_error,
        )
