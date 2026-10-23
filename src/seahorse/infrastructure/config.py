"""模型选择、随机范围及仿真步长的严格配置。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PointRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def validate_range(self) -> PointRange:
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum):
            raise ValueError("range must be finite")
        if self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        return self


class TurbineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1)
    model: Literal["random", "simple"] = "random"
    rated_power_kw: float = Field(default=2000, gt=0, allow_inf_nan=False)
    points: dict[str, PointRange] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_points(self) -> TurbineConfig:
        if self.model == "random" and not self.points:
            raise ValueError("random turbine requires configured points")
        return self


class WindFieldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["random", "simple"] = "random"
    mean_speed_mps: float = Field(default=8, ge=0, allow_inf_nan=False)
    amplitude_mps: float = Field(default=2, ge=0, allow_inf_nan=False)
    period_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    turbulence_std_mps: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    correlation_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    direction_deg: float = Field(default=270, ge=0, lt=360, allow_inf_nan=False)


class GridConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["random", "simple"] = "random"
    frequency_hz: float = Field(default=50, gt=0, allow_inf_nan=False)
    voltage_v: float = Field(default=35000, gt=0, allow_inf_nan=False)


class SubstationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["random", "simple"] = "random"
    active_loss_ratio: float = Field(default=0.02, ge=0, lt=1, allow_inf_nan=False)
    reactive_loss_ratio: float = Field(default=0.02, ge=0, lt=1, allow_inf_nan=False)


class StationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["random", "simple"] = "random"
    enabled: bool = False
    secondary_enabled: bool = False
    period_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    duration_seconds: float = Field(default=8.0, gt=0, allow_inf_nan=False)
    target_kw: float = Field(default=0.0, allow_inf_nan=False)
    delta_kw: float = Field(default=2000.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_window(self) -> StationConfig:
        if self.duration_seconds >= self.period_seconds:
            raise ValueError("event duration must be less than period")
        return self


class SeahorseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)
    seed: int = 42
    wind_field: WindFieldConfig = Field(default_factory=WindFieldConfig)
    grid: GridConfig = Field(default_factory=GridConfig)
    substation: SubstationConfig = Field(default_factory=SubstationConfig)
    turbines: list[TurbineConfig] = Field(min_length=1)
    dispatch_station: StationConfig = Field(default_factory=StationConfig)
    primary_station: StationConfig = Field(default_factory=StationConfig)
    inertia_station: StationConfig = Field(default_factory=StationConfig)
    dispatch_points: dict[str, PointRange]
    primary_points: dict[str, PointRange]
    inertia_points: dict[str, PointRange]

    @model_validator(mode="after")
    def validate_ids(self) -> SeahorseConfig:
        ids = [turbine.device_id for turbine in self.turbines]
        reserved = {"substation", "dispatch", "primary", "inertia"}
        if len(set(ids)) != len(ids) or any(device_id in reserved for device_id in ids):
            raise ValueError("duplicate or reserved device ID")
        if self.substation.model == "simple" and any(
            turbine.model != "simple" for turbine in self.turbines
        ):
            raise ValueError(
                "simple substation requires all turbine models to report physical power"
            )
        if (
            self.primary_station.enabled
            and self.inertia_station.enabled
            and self.primary_station.period_seconds == self.inertia_station.period_seconds
        ):
            raise ValueError("primary/inertia events require non-overlapping schedules")
        return self


def load_config(path: Path) -> SeahorseConfig:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SeahorseConfig.model_validate(raw)
