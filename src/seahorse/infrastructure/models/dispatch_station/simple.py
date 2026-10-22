"""AGC/AVC 周期目标，生成外部调度参考而不执行场级分配。"""
from __future__ import annotations

import math

from seahorse.domain.models import DeviceSnapshot, SimulationTime


class SimpleDispatchStation:
    def __init__(
        self,
        active_mean_kw: float = 60000.0,
        active_amplitude_kw: float = 10000.0,
        reactive_mean_kvar: float = 5000.0,
        reactive_amplitude_kvar: float = 2000.0,
        active_period_seconds: float = 300.0,
        reactive_period_seconds: float = 180.0,
        farm_rated_kw: float = 100000.0,
    ) -> None:
        self._params = (
            active_mean_kw, active_amplitude_kw, reactive_mean_kvar,
            reactive_amplitude_kvar, active_period_seconds, reactive_period_seconds,
        )
        self._rated = farm_rated_kw
        self._snapshot = DeviceSnapshot("dispatch", {})

    def step(self, time: SimulationTime) -> None:
        p, amp_p, q, amp_q, period_p, period_q = self._params
        active = max(0.0, min(self._rated, p + amp_p * math.sin(
            2 * math.pi * time.seconds / period_p
        )))
        reactive = q + amp_q * math.sin(2 * math.pi * time.seconds / period_q)
        self._snapshot = DeviceSnapshot(
            "dispatch", {"emsGridDispatchAct": active, "emsGridDispatchReact": reactive}
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
