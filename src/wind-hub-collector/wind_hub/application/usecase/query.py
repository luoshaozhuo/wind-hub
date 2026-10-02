"""Query use case——只读查询的应用编排。

基于 :class:`~wind_hub.application.runtime.runtime.Runtime` 提供：

- 单点实时读（绕过采集循环直接调用协议驱动的 ``read``）；
- 设备列表与单设备信息；
- 系统状态快照（``status()``）。

本用例**不缓存** devices / points 静态副本，每次查询都经
Runtime 的当前注册表读取——热重载增删/重建组件后，查询立即看到最新对象。

``read_point`` 的错误语义：
- 设备不存在 → :class:`CommandError`（API 层映射为 404）。
- 点不存在 → :class:`CommandError`（同样 404）。
- 协议读失败 → :class:`ProtocolError` 原样上抛（API 层映射为 503）。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub.application.runtime.runtime import Runtime
from wind_hub_core.config.schema import DeviceConfig
from wind_hub.domain.model.device import DeviceInfo
from wind_hub_core.model.errors import CommandError, ProtocolError
from wind_hub_core.model.point import PointRef, PointValue


class AcquisitionInfo(BaseModel):
    """单个采集实例（Task Instance）的业务执行状态快照。

    与实例生命周期状态（RUNNING / STOPPED）和设备连接状态分维度：
    本模型只描述「这个采集实例最近跑得怎样」。
    """

    instance_id: str
    """Task Instance 标识（``{task_id}:{device_id}``）。"""

    task_id: str
    """来源 Task Definition。"""

    device_id: str
    """设备稳定标识。"""

    point_group: str
    """采集 point_group。"""

    running: bool = False
    """一次 collect 正在执行时为 True。"""

    consecutive_failures: int = 0
    """连续失败次数（partial 不算失败）。"""

    last_error: str | None = None
    """最近一次失败的简要描述。"""

    last_duration: float | None = None
    """最近一次 collect 耗时（秒，单调时钟口径）。"""


class SystemStatus(BaseModel):
    """QueryUseCase.status 返回的 Runtime 聚合状态快照。"""

    running: bool
    """Runtime 已启动时为 True。"""

    device_count: int
    """当前 Runtime 设备数。"""

    sink_count: int
    """当前 Runtime Sink 数。"""

    devices_connected: int = 0
    """当前 health() 为 healthy 的设备数。"""

    sinks_healthy: int = 0
    """当前 health() 为 healthy 的 Sink 数。"""

    points_collected: int = 0
    """累计采集点数（调度器单调计数，进程重启归零）。"""

    points_routed: int = 0
    """累计路由点数——成功进入 sink 队列的点值总数。"""

    points_dropped: int = 0
    """累计丢弃点数——背压策略丢弃的点值总数。"""

    acquisitions: list[AcquisitionInfo] = []
    """各采集实例的业务执行状态（按 Task Instance 粒度）。"""


class QueryUseCase:
    """只读查询用例——所有读取都穿透到 Runtime 当前状态。

    Runtime 的热重载是就地增删 ``devices`` 注册表（Device 聚合配置、
    点表与协议实例），因此本用例持有的唯一引用就是 Runtime 本身，
    天然免疫「静态快照失效」问题。
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """实时读取单个点，绕过采集缓存直接走协议驱动。

        Args:
            device_id: 设备稳定标识。
            point_id: 点表 point_id。

        Returns:
            协议驱动返回的 PointValue。

        Raises:
            CommandError: 设备或点未知。
            ProtocolError: 强制重连失败、协议驱动读失败或未返回值。
        """
        device = self._runtime.devices.get(device_id)
        if device is None:
            raise CommandError(f"unknown device '{device_id}'", "")

        if not any(p.point_id == point_id for p in device.points):
            raise CommandError(f"unknown point '{device_id}/{point_id}'", "")

        if not await self._runtime.ensure_connected(device_id, force=True):
            raise ProtocolError(f"device '{device_id}' is not connected")
        try:
            values = await device.read_points([PointRef(device_id=device_id, point_id=point_id)])
        except Exception as exc:
            self._runtime.report_read_failure(device_id, exc)
            raise
        if not values:
            error = ProtocolError(f"read returned no value for '{device_id}/{point_id}'")
            self._runtime.report_read_failure(device_id, error)
            raise error
        self._runtime.report_read_success(device_id)
        return values[0]

    async def list_devices(self) -> list[DeviceInfo]:
        """返回当前注册表中全部设备运行状态。

        Returns:
            DeviceInfo 列表；热重载后立即反映当前 Runtime。
        """
        return [
            self._device_info(device_id, device.config)
            for device_id, device in self._runtime.devices.items()
        ]

    async def get_device_info(self, device_id: str) -> DeviceInfo:
        """返回单设备运行时状态。

        Args:
            device_id: 设备稳定标识。

        Returns:
            DeviceInfo。

        Raises:
            CommandError: 设备未知。
        """
        device = self._runtime.devices.get(device_id)
        if device is None:
            raise CommandError(f"unknown device '{device_id}'", "")
        return self._device_info(device_id, device.config)

    async def status(self) -> SystemStatus:
        """返回 Runtime 聚合状态快照。

        从 Runtime 聚合：
        - ``running``：运行时就绪标志。
        - ``device_count`` / ``sink_count``：Runtime 持有的组件总数。
        - ``devices_connected`` / ``sinks_healthy``：按 ``health()`` 返回的
          「设备优先、随后 sink」顺序，依 ``device_count`` 切分后统计健康数。
        - 点位统计：采集计数经 Runtime 透传自 AcquisitionEngine，路由/丢弃
          计数来自 Runtime 的 Sink 派发侧。
        - ``acquisitions``：各采集实例的业务执行状态（Runtime 的
          AcquisitionRuntimeState 快照）——与设备连接状态、实例启停状态
          分维度。
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
                    instance_id=state.instance_id,
                    task_id=state.task_id,
                    device_id=state.device_id,
                    point_group=state.point_group,
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
        device = self._runtime.devices.get(device_id)
        state = self._runtime.device_state(device_id)
        # connected 以驱动实时 health 为准（驱动自带重连监控时比 Runtime
        # 的记账更新）；consecutive_failures/last_error 来自 Runtime 的
        # 重连节流状态。
        connected = device.health().healthy if device is not None else False
        return DeviceInfo(
            device_id=device_id,
            protocol=cfg.protocol,
            connected=connected,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            last_error=state.last_error if state is not None else None,
        )
