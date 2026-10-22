"""严格验证随机设备组合配置及采样边界。"""
from __future__ import annotations

import math
from pathlib import Path

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
    points: dict[str, PointRange] = Field(min_length=1)


class SeahorseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)
    seed: int = 42
    turbines: list[TurbineConfig] = Field(min_length=1)
    dispatch_points: dict[str, PointRange]
    primary_points: dict[str, PointRange]
    inertia_points: dict[str, PointRange]

    @model_validator(mode="after")
    def validate_ids(self) -> SeahorseConfig:
        ids = [turbine.device_id for turbine in self.turbines]
        reserved = {"substation", "dispatch", "primary", "inertia"}
        if len(set(ids)) != len(ids) or any(device_id in reserved for device_id in ids):
            raise ValueError("duplicate or reserved device ID")
        return self


def load_config(path: Path) -> SeahorseConfig:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SeahorseConfig.model_validate(raw)
