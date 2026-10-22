"""按设备类型装配一期随机模型组合。"""
from seahorse.application.runtime import SeahorseRuntime
from seahorse.application.wind_farm_service import WindFarmService
from seahorse.infrastructure.config import PointRange, SeahorseConfig
from seahorse.infrastructure.models.dispatch_station.random import RandomDispatchStation
from seahorse.infrastructure.models.grid.random import RandomGrid
from seahorse.infrastructure.models.inertia_station.random import RandomInertiaStation
from seahorse.infrastructure.models.primary_control_station.random import (
    RandomPrimaryControlStation,
)
from seahorse.infrastructure.models.substation.random import RandomSubstation
from seahorse.infrastructure.models.wind_field.random import RandomWindField
from seahorse.infrastructure.models.wind_turbine.random import RandomTurbine
from seahorse.infrastructure.publisher import MemoryPublisher


def _ranges(points: dict[str, PointRange]) -> dict[str, tuple[float, float]]:
    return {name: (value.minimum, value.maximum) for name, value in points.items()}


def create_runtime(config: SeahorseConfig) -> SeahorseRuntime:
    seed = config.seed
    service = WindFarmService(
        wind_field=RandomWindField(seed),
        turbines=tuple(
            RandomTurbine(turbine.device_id, seed + 100 + index, _ranges(turbine.points))
            for index, turbine in enumerate(config.turbines)
        ),
        grid=RandomGrid(seed + 1),
        substation=RandomSubstation(seed + 2),
        dispatch=RandomDispatchStation(seed + 3, _ranges(config.dispatch_points)),
        primary=RandomPrimaryControlStation(seed + 4, _ranges(config.primary_points)),
        inertia=RandomInertiaStation(seed + 5, _ranges(config.inertia_points)),
        publisher=MemoryPublisher(),
    )
    return SeahorseRuntime(service, config.step_seconds)
