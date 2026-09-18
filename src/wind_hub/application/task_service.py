"""Task service — 生命周期控制与运行时状态聚合。

将 :class:`~wind_hub.domain.engine.scheduler.Scheduler` 与
:class:`~wind_hub.application.config_service.ConfigService` 包装为
:class:`~wind_hub.domain.port.inbound.TaskUseCase`，供 CLI 与 Web API 适配器
控制引擎启停、触发热重载、查询运行时状态快照。

``status()`` 直接从 scheduler 聚合真实状态（设备/sink 健康、总数、运行标志），
不做任何网络探测——粒度保持与 ``/health`` 一致。
"""

from __future__ import annotations

from wind_hub.application.config_service import ConfigService
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.port.inbound import SystemStatus, TaskUseCase


class TaskService(TaskUseCase):
    """任务与生命周期控制服务——委托给 Scheduler / ConfigService。

    ``reload_config`` 转发给 :class:`ConfigService`，并把 ``ReloadResult``
    的 ``success`` 折叠为 :class:`ConfigError`：CLI / API 通过异常区分成败。
    """

    def __init__(
        self,
        scheduler: Scheduler,
        config_service: ConfigService | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._config_service = config_service

    async def start(self) -> None:
        """启动引擎循环（幂等）。"""
        await self._scheduler.start()

    async def stop(self) -> None:
        """优雅停机（幂等）。"""
        await self._scheduler.stop()

    async def reload_config(self) -> None:
        """触发一次配置热重载，失败时抛 :class:`ConfigError`。

        Raises:
            ConfigError: 未配置 ConfigService，或热重载失败。
        """
        if self._config_service is None:
            raise ConfigError("config service is not wired into TaskService")
        result = await self._config_service.reload()
        if not result.success:
            details = "; ".join(result.errors) if result.errors else "unknown error"
            raise ConfigError(f"configuration reload failed: {details}")

    async def status(self) -> SystemStatus:
        """返回系统运行时快照。

        从 scheduler 聚合：
        - ``running``：调度器运行标志。
        - ``device_count`` / ``sink_count``：调度器持有的组件总数。
        - ``devices_connected`` / ``sinks_healthy``：按 ``health()`` 返回的
          「设备优先、随后 sink」顺序，依 ``device_count`` 切分后统计健康数。
        """
        health_values = list(self._scheduler.health().values())
        device_health = health_values[: self._scheduler.device_count]
        sink_health = health_values[self._scheduler.device_count :]

        devices_connected = sum(1 for h in device_health if h.healthy)
        sinks_healthy = sum(1 for h in sink_health if h.healthy)

        return SystemStatus(
            running=self._scheduler.running,
            device_count=self._scheduler.device_count,
            sink_count=self._scheduler.sink_count,
            devices_connected=devices_connected,
            sinks_healthy=sinks_healthy,
            points_collected=self._scheduler.points_collected,
            points_routed=self._scheduler.points_routed,
            points_dropped=self._scheduler.points_dropped,
        )
