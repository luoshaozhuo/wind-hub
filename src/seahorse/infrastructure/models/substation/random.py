"""随机升压站测量适配器，不声称功率守恒。"""
import random

from seahorse.domain.models import DeviceSnapshot, GridSample, SimulationTime


class RandomSubstation:
    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self._snapshot = DeviceSnapshot("substation", {})

    def step(
        self, time: SimulationTime, turbines: tuple[DeviceSnapshot, ...], grid: GridSample
    ) -> None:
        self._snapshot = DeviceSnapshot(
            "substation",
            {
                "poi_voltage": grid.voltage_v,
                "grid_frequency": grid.frequency_hz,
                "sample": self._rng.uniform(0, 1),
            },
        )

    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot
