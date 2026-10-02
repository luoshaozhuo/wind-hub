"""ADS device-notification 订阅连接池与线程安全回调边界。

pyads 在 worker thread 调用 notification callback，因此 callback 只允许把数据
写入线程安全队列，并通过 event loop 安全入口调度 drain；不得直接调用 ADS API、
执行协程或修改 Driver 状态。

一个 ADSSubscription 拥有自己的 connection pool，每条 connection 的 handle
数量受 max_notifications_per_connection 限制。subscribe 根据 point_id 和已解析
index 地址的差异增删注册；close 注销 notification 并关闭全部 connection。

pyads 回调对象缺少稳定类型标注，因此 connection/handle/callback 参数局部使用
Any；这些类型不会进入 ProtocolPort 或领域模型。
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from wind_hub.adapter.outbound.protocol.ads.config import ADSConfig
from wind_hub.adapter.outbound.protocol.ads.mapping import ADSPoint
from wind_hub_core.model.point import PointValue, Quality


def _pyads() -> Any:
    """延迟导入 pyads；Any 仅用于未类型化第三方边界。"""
    # pyads 尚未提供稳定类型声明；待上游 typing 可用后移除抑制。
    import pyads  # type: ignore[import-untyped]

    return pyads


def _plc_datatype(ads_name: str) -> Any:
    """返回 ADS 类型名对应的 pyads PLCTYPE；第三方 ctypes 类型以 Any 隔离。"""
    return getattr(_pyads(), f"PLCTYPE_{ads_name}")


class ADSSubscription:
    """管理 ADS notification connection pool 并把推送值交回事件循环。

    Args:
        config: 已解析的 ADS 连接与订阅参数。
        device_id: 写入 PointValue 的设备标识。
        host: PLC IP/主机名。
        loop: on_data 所属事件循环。
        on_data: 每个推送 PointValue 的异步回调。
        cycle_time: notification 周期，来自 Task.interval。
    """

    def __init__(
        self,
        config: ADSConfig,
        device_id: str,
        host: str,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[PointValue], Awaitable[None]],
        cycle_time: float,
    ) -> None:
        self._config = config
        self._device_id = device_id
        self._host = host
        self._loop = loop
        self._on_data = on_data
        # Notification cycle time（秒）——来自 Task.interval，由调用方在
        # 订阅建立时传入；不再是设备级配置。
        self._cycle_time = cycle_time

        # point_id 到已解析 ADSPoint；运行期注册不再依赖 symbol。
        self._points: dict[str, ADSPoint] = {}
        # pyads connection 与其 handle 计数使用平行列表维护。
        self._connections: list[Any] = []
        self._loads: list[int] = []
        # point_id 到 (connection index, notification handle, user handle) 的注册表。
        self._handles: dict[str, tuple[int, Any, Any]] = {}

        # worker thread 只写线程安全队列；事件循环负责后续异步分发。
        self._queue: queue.Queue[PointValue] = queue.Queue()
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def subscribe(self, points: list[ADSPoint]) -> None:
        """同步目标订阅集合，只增删差异项。

        Args:
            points: 已解析 index_group/index_offset 的目标 ADSPoint 列表。
                未解析点不会注册 notification。
        """
        new_points = {ap.point_id: ap for ap in points if ap.address_resolved}
        current = self._points

        removed = [
            point_id
            for point_id, point in current.items()
            if point_id not in new_points or new_points[point_id] != point
        ]
        added = [
            point
            for point_id, point in new_points.items()
            if point_id not in current or current[point_id] != point
        ]

        if removed:
            await self._unregister(removed)
        if added:
            await self._register(added)

        self._points = new_points

    async def unsubscribe_all(self) -> None:
        """注销当前实例的全部点订阅。"""
        await self._unregister(list(self._points))
        self._points = {}

    async def close(self) -> None:
        """注销全部 notification 并关闭 connection pool；重复释放异常被隔离。"""
        self._closed = True
        await self._unregister(list(self._points))
        self._points = {}
        for conn in self._connections:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(conn.close)
        self._connections = []
        self._loads = []

    # ------------------------------------------------------------------
    # connection pool 与 notification 注册
    # ------------------------------------------------------------------

    def _connection_for(self) -> int:
        """返回首个仍有 handle 容量的 connection 索引；不存在时返回 -1。"""
        max_per = self._config.max_notifications_per_connection
        for idx, load in enumerate(self._loads):
            if load < max_per:
                return idx
        return -1

    async def _create_connection(self) -> Any:
        """在线程池中创建并打开新的 pyads connection。"""
        pyads = _pyads()
        net_id = self._config.target_net_id or None
        conn = pyads.Connection(net_id, self._config.target_port, self._host)
        conn.set_timeout(int(self._config.timeout * 1000))
        await asyncio.to_thread(conn.open)
        return conn

    async def _register(self, points: list[ADSPoint]) -> None:
        """按 index_group/index_offset 注册 notification，并管理连接容量。"""
        pyads = _pyads()
        for ap in points:
            idx = self._connection_for()
            if idx < 0:
                conn = await self._create_connection()
                self._connections.append(conn)
                self._loads.append(0)
                idx = len(self._connections) - 1
            conn = self._connections[idx]
            attr = pyads.NotificationAttrib(
                max(ap.size, 1),
                cycle_time=self._cycle_time,
                max_delay=self._config.max_delay,
            )
            callback = conn.notification(_plc_datatype(ap.data_type))(
                self._notification_callback(ap)
            )
            handle, user_handle = await asyncio.to_thread(
                conn.add_device_notification,
                (ap.index_group, ap.index_offset),
                attr,
                callback,
            )
            self._loads[idx] += 1
            self._handles[ap.point_id] = (idx, handle, user_handle)

    async def _unregister(self, point_ids: list[str]) -> None:
        """注销指定 point_id 的 notification，并释放对应 handle 配额。"""
        for point_id in point_ids:
            if point_id not in self._handles:
                continue
            idx, handle, user_handle = self._handles.pop(point_id)
            conn = self._connections[idx]
            with contextlib.suppress(Exception):
                await asyncio.to_thread(conn.del_device_notification, handle, user_handle)
            self._loads[idx] = max(0, self._loads[idx] - 1)

    # ------------------------------------------------------------------
    # worker thread 回调到 event loop 的安全交接
    # ------------------------------------------------------------------

    def _notification_callback(self, point: ADSPoint) -> Callable[..., None]:
        """为一个已解析点创建 pyads notification 回调并固定 point_id。"""

        def callback(handle: Any, address: Any, timestamp: Any, value: Any) -> None:
            self._on_notification(point, handle, address, timestamp, value)

        return callback

    def _on_notification(
        self,
        point: ADSPoint,
        handle: Any,
        address: Any,
        timestamp: Any,
        value: Any,
    ) -> None:
        """接收 pyads worker thread 的 notification 回调。

        point 在注册时已经绑定，因此回调不依赖 symbol 名称反查。address 为
        pyads 返回的 index_group/index_offset 地址；这些未类型化第三方值
        仅停留在适配器边界。
        """
        if timestamp is None:
            timestamp = datetime.now(UTC)
        pv = PointValue(
            device_id=self._device_id,
            point_id=point.point_id,
            value=value,
            quality=Quality.GOOD,
            timestamp=timestamp,
            source="ads",
        )
        self._queue.put(pv)
        self._loop.call_soon_threadsafe(self._schedule_drain)

    def _schedule_drain(self) -> None:
        """在所属 event loop 上调度 drain；关闭后不再创建新任务。"""
        if self._closed:
            return
        asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """在所属 event loop 中排空队列，并顺序调用 on_data。"""
        while True:
            try:
                pv = self._queue.get_nowait()
            except queue.Empty:
                return
            await self._on_data(pv)
