"""Runtime 的设备与 Sink 健康状态聚合。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wind_hub_core.model.health import HealthStatus

if TYPE_CHECKING:
    from wind_hub.application.runtime.runtime import Runtime


class RuntimeHealth:
    """把 Device.health 与 Sink.health 聚合为统一 HealthStatus 映射。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def health(self) -> dict[str, HealthStatus]:
        """聚合当前全部 Device 与 Sink 的缓存健康状态。

        Returns:
            以组件名为键的 HealthStatus 映射；设备项先写入，随后为 Sink。
        """
        result: dict[str, HealthStatus] = {}
        for device_id, device in self._runtime._devices.items():
            result[device_id] = device.health()
        for name, sink in self._runtime._sinks.items():
            if name in self._runtime._unhealthy_sinks:
                result[name] = HealthStatus(healthy=False, message="open failed")
            else:
                result[name] = sink.health()
        return result
