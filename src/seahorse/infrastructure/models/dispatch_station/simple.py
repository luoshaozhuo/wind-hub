"""受限周期 AGC/AVC 目标生成器，不实现 EMS 分配。"""

from __future__ import annotations

import math

from seahorse.domain.models import DeviceSnapshot, SimulationTime


class SimpleDispatchStation:
    def __init__(
        self,
        active_mean_kw: float = 0.0,
        active_amplitude_kw: float = 0.0,
        reactive_mean_kvar: float = 0.0,
        reactive_amplitude_kvar: float = 0.0,
        active_period_seconds: float = 300.0,
        reactive_period_seconds: float = 180.0,
        farm_rated_kw: float = 100000.0,
    ) -> None:
        if active_period_seconds <= 0 or reactive_period_seconds <= 0 or farm_rated_kw <= 0:
            raise ValueError("period and farm rating must be positive")
        self._params = (
            active_mean_kw,
            active_amplitude_kw,
            reactive_mean_kvar,
            reactive_amplitude_kvar,
            active_period_seconds,
            reactive_period_seconds,
        )
        self._rated = farm_rated_kw
        self._snapshot = DeviceSnapshot("dispatch", {})

    def step(self, time: SimulationTime) -> None:
        p, ap, q, aq, tp, tq = self._params
        active = max(0.0, min(self._rated, p + ap * math.sin(2 * math.pi * time.seconds / tp)))
        reactive = q + aq * math.sin(2 * math.pi * time.seconds / tq)
        self._snapshot = DeviceSnapshot(
            "dispatch", {"emsGridDispatchAct": active, "emsGridDispatchReact": reactive}
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
