"""汇总已测风机功率并施加简化站内损耗。"""

from __future__ import annotations

import math

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class SimpleSubstation:
    def __init__(self, active_loss_ratio: float = 0.02, reactive_loss_ratio: float = 0.02) -> None:
        if not 0 <= active_loss_ratio < 1 or not 0 <= reactive_loss_ratio < 1:
            raise ValueError("loss ratios must be in [0, 1)")
        self._active_loss = active_loss_ratio
        self._reactive_loss = reactive_loss_ratio
        self._snapshot = DeviceSnapshot("substation", {})

    def step(
        self, time: SimulationTime, turbines: tuple[DeviceSnapshot, ...], grid: GridSample
    ) -> None:
        active = sum(float(t.points["emsPowAct"]) for t in turbines)
        reactive = sum(float(t.points["emsPowReact"]) for t in turbines)
        poi_active = max(0.0, active * (1 - self._active_loss))
        poi_reactive = reactive * (1 - self._reactive_loss)
        apparent = math.hypot(poi_active, poi_reactive)
        current = 1000 * apparent / (math.sqrt(3) * grid.voltage_v)
        self._snapshot = DeviceSnapshot(
            "substation",
            {
                "emsSyzAct": poi_active,
                "emsSyzReact": poi_reactive,
                "poi_voltage": grid.voltage_v,
                "poi_current": current,
                "grid_frequency": grid.frequency_hz,
            },
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
