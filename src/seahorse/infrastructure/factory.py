"""按设备分别选择随机或简化物理模型，无模型算法顶层分组。"""

from __future__ import annotations

from seahorse.application.command_queue import CommandQueue
from seahorse.application.runtime import SeahorseRuntime
from seahorse.application.wind_farm_service import WindFarmService
from seahorse.infrastructure.config import PointRange, SeahorseConfig
from seahorse.infrastructure.models.dispatch_station.random import RandomDispatchStation
from seahorse.infrastructure.models.dispatch_station.simple import SimpleDispatchStation
from seahorse.infrastructure.models.grid.random import RandomGrid
from seahorse.infrastructure.models.grid.simple import SimpleGrid
from seahorse.infrastructure.models.inertia_station.random import RandomInertiaStation
from seahorse.infrastructure.models.inertia_station.simple import SimpleInertiaStation
from seahorse.infrastructure.models.primary_control_station.random import (
    RandomPrimaryControlStation,
)
from seahorse.infrastructure.models.primary_control_station.simple import (
    SimplePrimaryControlStation,
)
from seahorse.infrastructure.models.substation.random import RandomSubstation
from seahorse.infrastructure.models.substation.simple import SimpleSubstation
from seahorse.infrastructure.models.wind_field.random import RandomWindField
from seahorse.infrastructure.models.wind_field.simple import SimpleWindField
from seahorse.infrastructure.models.wind_turbine.random import RandomTurbine
from seahorse.infrastructure.models.wind_turbine.simple import SimpleWindTurbine
from seahorse.infrastructure.publisher import MemoryPublisher
from seahorse.ports.grid import GridPort
from seahorse.ports.publisher import PublisherPort
from seahorse.ports.substation import SubstationPort
from seahorse.ports.wind_field import WindFieldPort
from seahorse.ports.wind_turbine import WindTurbinePort


def _ranges(points: dict[str, PointRange]) -> dict[str, tuple[float, float]]:
    return {name: (value.minimum, value.maximum) for name, value in points.items()}


def create_runtime(
    config: SeahorseConfig,
    publisher: PublisherPort | None = None,
    commands: CommandQueue | None = None,
) -> SeahorseRuntime:
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
                RandomTurbine(turbine.device_id, seed + 100 + index, turbine.points)
            )
    service = WindFarmService(
        wind_field=wind_field,
        turbines=tuple(turbines),
        grid=grid,
        substation=substation,
        dispatch=(
            SimpleDispatchStation(farm_rated_kw=sum(t.rated_power_kw for t in config.turbines))
            if config.dispatch_station.model == "simple"
            else RandomDispatchStation(seed + 3, _ranges(config.dispatch_points))
        ),
        primary=(
            SimplePrimaryControlStation(
                frequency_enabled=config.primary_station.enabled,
                voltage_enabled=config.primary_station.secondary_enabled,
                event_period_seconds=config.primary_station.period_seconds,
                frequency_event_seconds=config.primary_station.duration_seconds,
                active_target_kw=config.primary_station.target_kw,
                active_delta_kw=config.primary_station.delta_kw,
            )
            if config.primary_station.model == "simple"
            else RandomPrimaryControlStation(seed + 4, _ranges(config.primary_points))
        ),
        inertia=(
            SimpleInertiaStation(
                enabled=config.inertia_station.enabled,
                period_seconds=config.inertia_station.period_seconds,
                duration_seconds=config.inertia_station.duration_seconds,
                target_kw=config.inertia_station.target_kw,
            )
            if config.inertia_station.model == "simple"
            else RandomInertiaStation(seed + 5, _ranges(config.inertia_points))
        ),
        publisher=publisher if publisher is not None else MemoryPublisher(),
    )
    return SeahorseRuntime(service, config.step_seconds, commands)
