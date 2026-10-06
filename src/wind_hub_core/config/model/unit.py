"""units.yaml 单位定义模型（配置元数据）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from wind_hub_core.model.errors import ConfigError


class UnitConfig(BaseModel):
    """单个单位定义——``units.yaml`` 中 ``units`` 字典的值。

    点（:class:`~wind_hub_core.config.model.point.PointConfig`）经 unit ID
    （``units`` 的键）引用单位；``symbol`` 是展示层使用的显示符号
    （``kilowatt → kW``）。
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    """显示符号（如 ``'kW'``）；允许空字符串表示无量纲。"""

    name: str | None = None
    """单位显示名（如 ``'Kilowatt'``）。"""


class UnitsConfig(BaseModel):
    """Raw YAML root model——``units.yaml`` 顶层单位定义（文件级 wrapper）。

    unit ID 唯一性由 dict 键自然保证；ID 非空与 ``none`` 单位存在性在此
    校验。最终 resolved ``Config.units`` 是扁平的
    ``dict[unit_id, UnitConfig]``，不经过本类型。
    """

    model_config = ConfigDict(extra="forbid")

    units: dict[str, UnitConfig]

    @model_validator(mode="after")
    def _validate_units(self) -> UnitsConfig:
        for unit_id in self.units:
            if not unit_id.strip():
                raise ConfigError("units: unit ID must be non-empty")
        if "none" not in self.units:
            raise ConfigError("units: must define the 'none' (dimensionless) unit")
        return self


__all__ = [
    "UnitConfig",
    "UnitsConfig",
]
