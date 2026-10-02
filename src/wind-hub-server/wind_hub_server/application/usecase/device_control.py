"""设备写控制与真实回读用例。"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from wind_hub_core.model.command import Command
from wind_hub_server.application.port.point_store import LatestPointStore, TrendStore
from wind_hub_server.application.port.worker import CommanderPort


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
    """通过独立 Commander 完成设备写入与回读闭环。"""

    def __init__(
        self,
        commander: CommanderPort,
        latest: LatestPointStore,
        trend: TrendStore,
    ) -> None:
        self._commander = commander
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
        """写入设备；成功后经 Commander 即时回读同一点并刷新缓存。"""
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

        if result.success:
            try:
                observed = await self._commander.read_point(device_id, point_id)
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
