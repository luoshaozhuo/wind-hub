"""Seahorse 与 Gateway 同进程部署时的受控反馈发布适配器。

运行于仿真线程，Gateway 点存储在其 asyncio 主事件循环中更新。
只使用 PointStore.update_batch 的反馈路径，不经由命令通道伪造控制。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout

from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain import PointKey
from industrial_gateway.infrastructure.config.loader import GatewayConfig
from seahorse.domain.models import FarmSnapshot, Scalar


class GatewayStorePublisher:
    def __init__(
        self,
        store: PointStore,
        gateway: GatewayConfig,
        loop: asyncio.AbstractEventLoop,
        timeout_seconds: float = 3.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._store = store
        self._loop = loop
        self._timeout = timeout_seconds
        self._expected: frozenset[PointKey] = frozenset(
            (device_id, point_id)
            for device_id, device in gateway.devices.items()
            for point_id, point in gateway.point_tables[device.point_table_id].points.items()
            if point.role == "feedback"
        )
        if not self._expected:
            raise ValueError("Gateway has no feedback points")

    def publish(self, snapshot: FarmSnapshot) -> None:
        devices = {device.device_id: device.points for device in snapshot.devices}
        payload: dict[PointKey, Scalar] = {}
        for device_id, point_id in self._expected:
            device = devices.get(device_id)
            if device is None or point_id not in device:
                raise KeyError(f"missing model feedback: {device_id}.{point_id}")
            payload[(device_id, point_id)] = device[point_id]
        future = asyncio.run_coroutine_threadsafe(
            self._store.update_batch(payload, source="seahorse"), self._loop
        )
        try:
            future.result(timeout=self._timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError("Gateway feedback update uncertain") from exc
