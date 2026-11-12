"""Shared weather state: correlated wind, day irradiance and air temperature."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass

from data_generator.application.physical.common import (
    as_mapping,
    clamp,
    number,
    optional_number,
)
from data_generator.domain.model import GeneratorError


@dataclass(frozen=True, slots=True)
class Weather:
    wind_speed: float
    wind_direction: float
    air_density: float
    irradiance: float
    ambient_temperature: float


class WeatherModel:
    """One instance per weather reference; exactly one advance per time step."""

    def __init__(self, weather_id: str, config: Mapping[str, object]) -> None:
        self.id = weather_id
        self.wind = as_mapping(config.get("wind"), "weather.wind")
        self.sun = as_mapping(config.get("irradiance"), "weather.irradiance")
        self.heat = as_mapping(config.get("ambient_temperature"), "weather.temperature")
        self.mean = number(self.wind, "wind_speed_mean", minimum=0)
        self.direction = math.radians(number(self.wind, "wind_direction"))
        self.turbulence = number(self.wind, "turbulence_intensity", minimum=0)
        self.tau_wind = number(self.wind, "wind_correlation_time_s", minimum=0)
        self.density = number(self.wind, "air_density", minimum=0)
        if self.tau_wind == 0 or self.density == 0:
            raise GeneratorError("invalid weather wind correlation or air density")
        sunrise = number(self.sun, "sunrise_s", minimum=0)
        sunset = number(self.sun, "sunset_s", minimum=0)
        if not sunrise < sunset <= 86400:
            raise GeneratorError("invalid sunrise and sunset")
        self.sunrise, self.sunset = sunrise, sunset
        self.clear = number(self.sun, "irradiance_clear_w_m2", minimum=0)
        self.cloud = number(self.sun, "cloud_factor_target", minimum=0)
        if self.cloud > 1:
            raise GeneratorError("cloud factor must not exceed one")
        self.cloud_mode = str(self.sun.get("cloud_mode", "constant"))
        if self.cloud_mode not in {"constant", "correlated"}:
            raise GeneratorError("unknown cloud mode")
        self.cloud_tau = optional_number(self.sun, "cloud_tau_s", 10.0)
        self.cloud_sigma = optional_number(self.sun, "cloud_sigma", 0.0)
        if self.cloud_tau <= 0 or self.cloud_sigma < 0:
            raise GeneratorError("invalid cloud correlation")
        self.temperature_mean = number(self.heat, "temperature_mean_c")
        self.temperature_amplitude = number(self.heat, "diurnal_amplitude_k", minimum=0)
        self.temperature_tau = number(self.heat, "temperature_tau_s", minimum=0)
        self.temperature_sigma = number(self.heat, "temperature_sigma_k", minimum=0)
        self.temperature_phase = number(self.heat, "phase_rad")
        if self.temperature_tau == 0:
            raise GeneratorError("temperature time constant must be positive")
        self.wind_z = optional_number(self.wind, "z_initial", 0.0)
        self.temperature_z = optional_number(self.heat, "temperature_initial", 0.0)
        self.cloud_state = optional_number(self.sun, "cloud_initial", self.cloud)
        if not 0 <= self.cloud_state <= 1:
            raise GeneratorError("invalid initial cloud")
        self.wind_rng = self._random("wind", self.wind, "wind_seed")
        self.temperature_rng = self._random("temperature", self.heat, "temperature_seed")
        self.cloud_rng = self._random("cloud", self.sun, "cloud_seed")

    def _random(self, channel: str, settings: Mapping[str, object], key: str) -> random.Random:
        seed = settings.get(key, 0)
        if type(seed) is not int:
            raise GeneratorError(f"{key} must be integer")
        digest = hashlib.sha256(f"{self.id}:{channel}:{seed}".encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def advance(self, dt: float, time_s: float) -> Weather:
        if dt <= 0 or time_s < 0:
            raise GeneratorError("invalid weather time step")
        wind_a = math.exp(-dt / self.tau_wind)
        self.wind_z = wind_a * self.wind_z + math.sqrt(1 - wind_a**2) * self.wind_rng.gauss(0, 1)
        shape = math.sqrt(math.log1p(self.turbulence**2))
        wind_speed = self.mean * math.exp(clamp(shape * self.wind_z - shape**2 / 2, -100, 100))
        if self.cloud_mode == "correlated":
            cloud_a = math.exp(-dt / self.cloud_tau)
            self.cloud_state = clamp(
                cloud_a * self.cloud_state + (1 - cloud_a) * self.cloud
                + self.cloud_sigma * math.sqrt(1 - cloud_a**2) * self.cloud_rng.gauss(0, 1),
                0, 1,
            )
        clock = time_s % 86400
        daily = math.sin(math.pi * (clock - self.sunrise) / (self.sunset - self.sunrise)) if self.sunrise < clock < self.sunset else 0.0
        temp_a = math.exp(-dt / self.temperature_tau)
        self.temperature_z = temp_a * self.temperature_z + math.sqrt(1 - temp_a**2) * self.temperature_rng.gauss(0, 1)
        temperature = (
            self.temperature_mean + self.temperature_amplitude
            * math.sin(2 * math.pi * clock / 86400 + self.temperature_phase)
            + self.temperature_sigma * self.temperature_z
        )
        return Weather(wind_speed, self.direction, self.density, self.clear * daily * self.cloud_state, temperature)
