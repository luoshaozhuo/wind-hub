"""周期平均风、相关湍流和缓慢变化风向。"""

from __future__ import annotations

import math
import random

from seahorse.domain.models import SimulationTime, WindSample


class SimpleWindField:
    def __init__(
        self,
        seed: int,
        mean_speed_mps: float = 8.0,
        amplitude_mps: float = 2.0,
        period_seconds: float = 300.0,
        turbulence_std_mps: float = 0.5,
        correlation_seconds: float = 10.0,
        direction_deg: float = 270.0,
        direction_noise_deg: float = 0.2,
    ) -> None:
        self._rng = random.Random(seed)
        self._mean = mean_speed_mps
        self._amplitude = amplitude_mps
        self._period = period_seconds
        self._std = turbulence_std_mps
        self._correlation = correlation_seconds
        self._direction_noise = direction_noise_deg
        self._turbulence = 0.0
        self._direction = direction_deg
        self._sample = WindSample(mean_speed_mps, direction_deg)

    def step(self, time: SimulationTime) -> None:
        a = math.exp(-time.dt_seconds / self._correlation)
        self._turbulence = a * self._turbulence + self._std * math.sqrt(
            1 - a * a
        ) * self._rng.gauss(0, 1)
        self._direction = (
            self._direction
            + self._direction_noise * math.sqrt(time.dt_seconds) * self._rng.gauss(0, 1)
        ) % 360
        speed = max(
            0.0,
            self._mean
            + self._amplitude * math.sin(2 * math.pi * time.seconds / self._period)
            + self._turbulence,
        )
        self._sample = WindSample(speed, self._direction)

    def sample(self, device_id: str) -> WindSample:
        return self._sample
