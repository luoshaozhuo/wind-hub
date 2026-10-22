"""按设备分别选择随机或简化物理模型，无模型算法顶层分组。"""
from __future__ import annotations

from seahorse.application.runtime import SeahorseRuntime
from seahorse.application.wind_farm_service import WindFarmService
from seahorse.infrastructure.config import PointRange, SeahorseConfig
from seahorse.infrastructure.models.dispatch_station.random import RandomDispatchStation
from seahorse.infrastructure.models.grid.random import RandomGrid
from seahorse.infrastructure.models.grid.simple import SimpleGrid
from seahorse.infrastructure.models.inertia_station.random import RandomInertiaStation
from seahorse.infrastructure.models.primary_control_station.random import (
    RandomPrimaryControlStation,
)
from seahorse.infrastructure.models.substation.random import RandomSubstation
from seahorse.infrastructure.models.substation.simple import SimpleSubstation
from seahorse.infrastructure.models.wind_field.random import RandomWindField
from seahorse.infrastructure.models.wind_field.simple import SimpleWindField
from seahorse.infrastructure.models.wind_turbine.random import RandomTurbine
from seahorse.infrastructure.models.wind_turbine.simple import SimpleWindTurbine

from seahorse.infrastructure.publisher import MemoryPublisher
from seahorse.ports.grid import GridPort
from seahorse.ports.substation import SubstationPort
from seahorse.ports.wind_field import WindFieldPort
from seahorse.ports.wind_turbine import WindTurbinePort


def _ranges(points: dict[str, PointRange]) -> dict[str, tuple[float, float]]:
    return {name: (value.minimum, value.maximum) for name, value in points.items()}


def create_runtime(config: SeahorseConfig) -> SeahorseRuntime:
    seed = config.seed
    wind_field: WindFieldPort
    if config.wind_field.model == "simple":
        wind_field = SimpleWindField(
            seed,
            config.wind_field.mean_speed_mps,
            config.wind_field.amplitude_mps,
            config.wind_field.period_seconds,
            config.wind_field.turbulence_std_mps,
            config.wind_field.correlation_seconds,
            config.wind_field.direction_deg,
        )
    else:
        wind_field = RandomWindField(seed)

    grid: GridPort
    if config.grid.model == "simple":
        grid = SimpleGrid(config.grid.frequency_hz, config.grid.voltage_v)
    else:
        grid = RandomGrid(seed + 1)

    substation: SubstationPort
    if config.substation.model == "simple":
        substation = SimpleSubstation(
            config.substation.active_loss_ratio, config.substation.reactive_loss_ratio
        )
    else:
        substation = RandomSubstation(seed + 2)

    turbines: list[WindTurbinePort] = []
    for index, turbine in enumerate(config.turbines):
        if turbine.model == "simple":
            turbines.append(
                SimpleWindTurbine(turbine.device_id, seed + 100 + index, turbine.rated_power_kw)
            )
        else:
            turbines.append(
                RandomTurbine(turbine.device_id, seed + 100 + index, _ranges(turbine.points))
            )
    service = WindFarmService(
        wind_field=wind_field,
        turbines=tuple(turbines),
        grid=grid,
        substation=substation,
        dispatch=RandomDispatchStation(seed + 3, _ranges(config.dispatch_points)),
        primary=RandomPrimaryControlStation(seed + 4, _ranges(config.primary_points)),
        inertia=RandomInertiaStation(seed + 5, _ranges(config.inertia_points)),
        publisher=MemoryPublisher(),
    )
    return SeahorseRuntime(service, config.step_seconds)
