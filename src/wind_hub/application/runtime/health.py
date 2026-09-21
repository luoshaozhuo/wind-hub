"""Health aggregation for Runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wind_hub.domain.port.outbound import HealthStatus

if TYPE_CHECKING:
    from wind_hub.application.runtime.runtime import Runtime


class RuntimeHealth:
    """Aggregate device and sink health for the runtime."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def health(self) -> dict[str, HealthStatus]:
        result: dict[str, HealthStatus] = {}
        for device_id, proto in self._runtime._protocols.items():
            result[device_id] = proto.health()
        for name, sink in self._runtime._sinks.items():
            if name in self._runtime._unhealthy_sinks:
                result[name] = HealthStatus(healthy=False, message="open failed")
            else:
                result[name] = sink.health()
        return result
