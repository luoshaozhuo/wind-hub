"""Query use case——只读查询的应用编排。

基于 :class:`~wind_hub.application.runtime.runtime.Runtime` 提供：

- 单点实时读（绕过采集循环直接调用协议驱动的 ``read``）；
- 设备列表与单设备信息；
- 系统状态快照（``status()``）。

本用例**不缓存** protocols / devices / points 静态副本，每次查询都经
Runtime 的当前注册表读取——热重载增删/重建组件后，查询立即看到最新对象。

``read_point`` 的错误语义：
- 设备不存在 → :class:`CommandError`（API 层映射为 404）。
- 点不存在 → :class:`CommandError`（同样 404）。
- 协议读失败 → :class:`ProtocolError` 原样上抛（API 层映射为 503）。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub.application.runtime.runtime import Runtime
from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue


class AcquisitionInfo(BaseModel):
    """单个采集 Job（``(device, group)``）的业务执行状态快照。

    与调度器的 Job 状态（注册/暂停/下次触发时间）和设备连接状态分维度：
    本模型只描述「这个采集 Job 最近跑得怎样」。
    """

    device_id: str
    """Device identifier."""

    group: str
    """Polling group name."""

    running: bool = False
    """``True`` while a collect run is in flight."""

    consecutive_failures: int = 0
    """连续失败次数（partial 不算失败）。"""

    last_error: str | None = None
    """最近一次失败的简要描述。"""

    last_duration: float | None = None
    """最近一次 collect 耗时（秒，单调时钟口径）。"""


class SystemStatus(BaseModel):
    """Runtime status snapshot returned by :meth:`QueryUseCase.status`."""

    running: bool
    """``True`` when the engine loop is active."""

    device_count: int
    """Number of configured devices."""

    sink_count: int
    """Number of configured sinks."""

    devices_connected: int = 0
    """Number of devices currently reporting a healthy connection."""

    sinks_healthy: int = 0
    """Number of sinks currently reporting healthy."""

    points_collected: int = 0
    """累计采集点数（调度器单调计数，进程重启归零）。"""

    points_routed: int = 0
    """累计路由点数——成功进入 sink 队列的点值总数。"""

    points_dropped: int = 0
    """累计丢弃点数——背压策略丢弃的点值总数。"""

    acquisitions: list[AcquisitionInfo] = []
    """各采集 Job 的业务执行状态（按 ``(device, group)`` 粒度）。"""


class QueryUseCase:
    """只读查询用例——所有读取都穿透到 Runtime 当前状态。

    Runtime 的热重载是就地增删注册表（``devices`` / ``protocols`` /
    ``points_by_device``），因此本用例持有的唯一引用就是 Runtime 本身，
    天然免疫「静态快照失效」问题。
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """实时读取单个点，绕过采集缓存直接走协议驱动。

        Raises:
            CommandError: 设备或点未知。
            ProtocolError: 协议驱动读失败（设备不可达等）。
        """
        if device_id not in self._runtime.devices:
            raise CommandError(f"unknown device '{device_id}'", "")

        device_points = self._runtime.points_by_device.get(device_id, [])
        if not any(p.point_id == point_id for p in device_points):
            raise CommandError(f"unknown point '{device_id}/{point_id}'", "")

        proto = self._runtime.protocols.get(device_id)
        if proto is None:
            raise CommandError(f"no protocol driver for device '{device_id}'", "")

        values = await proto.read([PointRef(device_id=device_id, point_id=point_id)])
        if not values:
            raise ProtocolError(f"read returned no value for '{device_id}/{point_id}'")
        return values[0]

    async def list_devices(self) -> list[DeviceInfo]:
        """返回所有配置设备的运行时状态（按当前注册表）。"""
        return [
            self._device_info(device_id, cfg) for device_id, cfg in self._runtime.devices.items()
        ]

    async def get_device_info(self, device_id: str) -> DeviceInfo:
        """返回单设备运行时状态。

        Raises:
            CommandError: 设备未知。
        """
        cfg = self._runtime.devices.get(device_id)
        if cfg is None:
            raise CommandError(f"unknown device '{device_id}'", "")
        return self._device_info(device_id, cfg)

    async def status(self) -> SystemStatus:
        """返回系统运行时快照。

        从 Runtime 聚合：
        - ``running``：运行时就绪标志。
        - ``device_count`` / ``sink_count``：Runtime 持有的组件总数。
        - ``devices_connected`` / ``sinks_healthy``：按 ``health()`` 返回的
          「设备优先、随后 sink」顺序，依 ``device_count`` 切分后统计健康数。
        - 点位统计：采集计数经 Runtime 透传自 AcquisitionEngine，路由/丢弃
          计数来自 Runtime 的 Sink 派发侧。
        - ``acquisitions``：各采集 Job 的业务执行状态（Runtime 的
          AcquisitionRuntimeState 快照）——与设备连接状态、调度器 Job
          注册/暂停状态分维度。
        """
        health_values = list(self._runtime.health().values())
        device_health = health_values[: self._runtime.device_count]
        sink_health = health_values[self._runtime.device_count :]

        devices_connected = sum(1 for h in device_health if h.healthy)
        sinks_healthy = sum(1 for h in sink_health if h.healthy)

        return SystemStatus(
            running=self._runtime.running,
            device_count=self._runtime.device_count,
            sink_count=self._runtime.sink_count,
            devices_connected=devices_connected,
            sinks_healthy=sinks_healthy,
            points_collected=self._runtime.points_collected,
            points_routed=self._runtime.points_routed,
            points_dropped=self._runtime.points_dropped,
            acquisitions=[
                AcquisitionInfo(
                    device_id=state.device_id,
                    group=state.group,
                    running=state.running,
                    consecutive_failures=state.consecutive_failures,
                    last_error=state.last_error,
                    last_duration=state.last_duration,
                )
                for state in self._runtime.acquisition_states().values()
            ],
        )

    def _device_info(self, device_id: str, cfg: DeviceConfig) -> DeviceInfo:
        """从设备配置 + 协议健康状态 + Runtime 设备运行状态构造
        :class:`DeviceInfo`。

        ``last_seen`` 暂无逐设备读取墙钟时间戳追踪，恒为 ``None``（诚实
        空缺）；连接健康与重连计数来自 Runtime 的 DeviceRuntimeState。
        """
        proto = self._runtime.protocols.get(device_id)
        state = self._runtime.device_state(device_id)
        # connected 以驱动实时 health 为准（驱动自带重连监控时比 Runtime
        # 的记账更新）；consecutive_failures/last_error 来自 Runtime 的
        # 重连节流状态。
        connected = proto.health().healthy if proto is not None else False
        return DeviceInfo(
            device_id=device_id,
            protocol=cfg.protocol,
            connected=connected,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            last_error=state.last_error if state is not None else None,
        )
