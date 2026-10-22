"""短时惯量事件模型，与一次调频互斥由服务编排保障。"""
from __future__ import annotations

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class SimpleInertiaStation:
    def __init__(
        self, enabled: bool = False, period_seconds: float = 120.0,
        duration_seconds: float = 8.0, target_kw: float = 0.0,
    ) -> None:
        self._enabled = enabled
        self._period = period_seconds
        self._duration = duration_seconds
        self._target = target_kw
        self._snapshot = DeviceSnapshot("inertia", {})

    def step(self, time: SimulationTime, grid: GridSample) -> None:
        phase = time.seconds % self._period
        active = self._enabled and time.seconds > self._period and (
            time.dt_seconds <= phase < self._duration
        )
        self._snapshot = DeviceSnapshot(
            "inertia", {"actInertiaMode": active, "actInertiaDst": self._target}
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
