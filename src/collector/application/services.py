"""Collector 运行事实查询服务。

基于 :class:`CollectorRuntime` 的只读快照与实例生命周期委托：

- Query：设备注册表/连接状态与 Runtime 聚合状态；

所有读取都穿透 CollectorRuntime 当前状态，热重载后立即可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .runtime import CollectorRuntime


@dataclass(frozen=True, slots=True)
class AcquisitionInfo:
    """单个采集实例（Task Instance）的业务执行状态快照。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    running: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_duration: float | None = None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """单个设备的连接状态快照（``last_seen`` 暂无墙钟追踪，恒为 None）。"""

    device_id: str
    protocol: str
    connected: bool
    consecutive_failures: int = 0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class SystemStatus:
    """CollectorRuntime 聚合状态快照。"""

    running: bool
    device_count: int
    devices_connected: int = 0
    points_collected: int = 0
    points_routed: int = 0
    points_dropped: int = 0
    acquisitions: list[AcquisitionInfo] = field(default_factory=list)


class CollectorQueryService:
    """Collector 运行事实查询服务——所有读取都穿透 CollectorRuntime 当前状态。"""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

    async def list_devices(self) -> list[DeviceInfo]:
        """返回当前注册表中全部设备运行状态。"""
        return [self._device_info(device_id) for device_id in self._runtime.devices]

    async def status(self) -> SystemStatus:
        """返回 CollectorRuntime 聚合状态快照。"""
        devices_connected = sum(
            1 for session in self._runtime.devices.values() if session.is_open()
        )
        return SystemStatus(
            running=self._runtime.running,
            device_count=self._runtime.device_count,
            devices_connected=devices_connected,
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

    def _device_info(self, device_id: str) -> DeviceInfo:
        """从协议健康状态 + DeviceRuntime 设备运行状态构造 DeviceInfo。

        connected 以协议传输连接状态（is_open）为准；
        consecutive_failures/last_error 来自 DeviceRuntime 的重连节流状态。
        """
        session = self._runtime.devices.get(device_id)
        state = self._runtime.device_state(device_id)
        connected = session.is_open() if session is not None else False
        protocol = session.protocol_name if session is not None else ""
        return DeviceInfo(
            device_id=device_id,
            protocol=protocol,
            connected=connected,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            last_error=state.last_error if state is not None else None,
        )
