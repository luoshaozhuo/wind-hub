"""一次调频/调压事件信号，目标与使能分开发布。"""
from __future__ import annotations

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class SimplePrimaryControlStation:
    def __init__(
        self,
        frequency_enabled: bool = False,
        voltage_enabled: bool = False,
        event_period_seconds: float = 120.0,
        frequency_event_seconds: float = 20.0,
        active_target_kw: float = 0.0,
        active_delta_kw: float = 2000.0,
        reactive_target_kvar: float = 0.0,
    ) -> None:
        self._frequency_enabled = frequency_enabled
        self._voltage_enabled = voltage_enabled
        self._period = event_period_seconds
        self._duration = frequency_event_seconds
        self._active_target = active_target_kw
        self._delta = active_delta_kw
        self._reactive_target = reactive_target_kvar
        self._snapshot = DeviceSnapshot("primary", {})

    def step(self, time: SimulationTime, grid: GridSample) -> None:
        # 先提供参考目标，投入模式由下一个离散步触发。
        phase = time.seconds % self._period
        active = self._frequency_enabled and time.seconds > self._period and (
            time.dt_seconds <= phase < self._duration
        )
        self._snapshot = DeviceSnapshot(
            "primary",
            {
                "emsIsFreMod": active,
                "emsFreDst": self._active_target,
                "emsDeltapValue": self._delta if active else 0.0,
                "freReactMod": self._voltage_enabled,
                "freReactDst": self._reactive_target,
            },
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
