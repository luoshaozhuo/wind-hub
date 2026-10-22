"""单一服务编排所有虚拟设备，统一推进和发布。"""
from __future__ import annotations

import math

from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import FarmSnapshot, SimulationTime
from seahorse.ports.dispatch_station import DispatchStationPort
from seahorse.ports.grid import GridPort
from seahorse.ports.inertia_station import InertiaStationPort
from seahorse.ports.primary_control_station import PrimaryControlStationPort
from seahorse.ports.publisher import PublisherPort
from seahorse.ports.substation import SubstationPort
from seahorse.ports.wind_field import WindFieldPort
from seahorse.ports.wind_turbine import WindTurbinePort


class WindFarmService:
    def __init__(
        self,
        *,
        wind_field: WindFieldPort,
        turbines: tuple[WindTurbinePort, ...],
        substation: SubstationPort,
        grid: GridPort,
        dispatch: DispatchStationPort,
        primary: PrimaryControlStationPort,
        inertia: InertiaStationPort,
        publisher: PublisherPort,
    ) -> None:
        self._wind_field = wind_field
        self._turbines = {t.device_id: t for t in turbines}
        if len(self._turbines) != len(turbines):
            raise ValueError("duplicate turbine device_id")
        self._substation = substation
        self._grid = grid
        self._dispatch = dispatch
        self._primary = primary
        self._inertia = inertia
        self._publisher = publisher
        self._seen_commands: set[str] = set()
        self._last_step_index = 0

    def command(self, command: EquipmentCommand) -> None:
        if not command.command_id or not command.point_id:
            raise ValueError("command_id and point_id required")
        if isinstance(command.value, float) and not math.isfinite(command.value):
            raise ValueError("command value must be finite")
        if command.command_id in self._seen_commands:
            raise ValueError("duplicate command_id")
        turbine = self._turbines.get(command.device_id)
        if turbine is None:
            raise ValueError(f"unknown command device: {command.device_id}")
        turbine.apply(command)
        self._seen_commands.add(command.command_id)

    def step(self, time: SimulationTime) -> FarmSnapshot:
        if (
            time.step_index != self._last_step_index + 1
            or not math.isfinite(time.dt_seconds)
            or time.dt_seconds <= 0
        ):
            raise ValueError("simulation steps must be consecutive and positive")
        self._wind_field.step(time)
        self._grid.step(time)
        grid = self._grid.sample()
        self._dispatch.step(time)
        self._primary.step(time, grid)
        self._inertia.step(time, grid)
        primary_active = bool(self._primary.snapshot().points.get("emsIsFreMod", False))
        inertia_active = bool(self._inertia.snapshot().points.get("actInertiaMode", False))
        if primary_active and inertia_active:
            raise ValueError("primary frequency and inertia modes overlap")
        for turbine in self._turbines.values():
            turbine.step(time, self._wind_field.sample(turbine.device_id), grid)
        snapshots = tuple(turbine.snapshot() for turbine in self._turbines.values())
        self._substation.step(time, snapshots, grid)
        result = FarmSnapshot(
            time,
            (
                *snapshots,
                self._substation.snapshot(),
                self._dispatch.snapshot(),
                self._primary.snapshot(),
                self._inertia.snapshot(),
            ),
        )
        self._last_step_index = time.step_index
        self._publisher.publish(result)
        return result
