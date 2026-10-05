"""Query use case——只读查询的应用编排。

基于 :class:`~wind_hub_collector.application.runtime.runtime.CollectorRuntime` 提供：

- 当前设备注册表与连接状态；
- CollectorRuntime 聚合状态快照（``status()``）。

即时设备读写与现场协议诊断由独立 wind-hub-commander 负责；本用例只暴露
Collector 自身运行事实。所有查询都穿透 CollectorRuntime 当前状态，热重载后立即可见。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wind_hub_collector.application.runtime.runtime import CollectorRuntime
from wind_hub_collector.domain.model.device import DeviceInfo
from wind_hub_core.config import DeviceConfig


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
    """QueryUseCase.status 返回的 CollectorRuntime 聚合状态快照。"""

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

    acquisitions: list[AcquisitionInfo] = Field(default_factory=list)
    """各采集实例的业务执行状态（按 Task Instance 粒度）。"""


class QueryUseCase:
    """Collector 运行事实查询用例——所有读取都穿透 CollectorRuntime 当前状态。"""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

    async def list_devices(self) -> list[DeviceInfo]:
        """返回当前注册表中全部设备运行状态。

        Returns:
            DeviceInfo 列表；热重载后立即反映当前 CollectorRuntime。
        """
        return [
            self._device_info(device_id, device.config)
            for device_id, device in self._runtime.device_runtime.devices.items()
        ]

    async def status(self) -> SystemStatus:
        """返回 CollectorRuntime 聚合状态快照。

        从 CollectorRuntime 聚合：
        - ``running``：运行时就绪标志。
        - ``device_count`` / ``sink_count``：CollectorRuntime 持有的组件总数。
        - ``devices_connected`` / ``sinks_healthy``：按 ``health()`` 返回的
          「设备优先、随后 sink」顺序，依 ``device_count`` 切分后统计健康数。
        - 点位统计：采集计数经 CollectorRuntime 透传自 AcquisitionEngine，路由/丢弃
          计数来自 CollectorRuntime 的 Sink 派发侧。
        - ``acquisitions``：各采集实例的业务执行状态（CollectorRuntime 的
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
        """从设备配置 + 协议健康状态 + DeviceRuntime 设备运行状态构造
        :class:`DeviceInfo`。

        ``last_seen`` 暂无逐设备读取墙钟时间戳追踪，恒为 ``None``（诚实
        空缺）；连接健康与重连计数来自 DeviceRuntime 的 DeviceRuntimeState。
        """
        device = self._runtime.device_runtime.devices.get(device_id)
        state = self._runtime.device_runtime.device_state(device_id)
        # connected 以驱动实时 health 为准（驱动自带重连监控时比 CollectorRuntime
        # 的记账更新）；consecutive_failures/last_error 来自 DeviceRuntime 的
        # 重连节流状态。
        connected = device.health().healthy if device is not None else False
        return DeviceInfo(
            device_id=device_id,
            protocol=cfg.protocol,
            connected=connected,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            last_error=state.last_error if state is not None else None,
        )
