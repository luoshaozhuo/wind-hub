"""Commander 现场配置加载。

Commander 只读取即时设备操作所需的 system ADS、本地连接参数、设备实例、
设备型号和点表；不读取 Task、Sink、Reporting，也不依赖 Collector 配置包。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub_core.config.device_resolver import resolve_devices
from wind_hub_core.config.point_table_resolver import resolve_point_tables
from wind_hub_core.config.schema import (
    ADSSystemConfig,
    DeviceInstancesConfig,
    DeviceModelsConfig,
    DevicesConfig,
    PointConfig,
    PointTablesConfig,
    ResolvedPointTables,
)
from wind_hub_core.model.errors import ConfigError


@dataclass(frozen=True, slots=True)
class CommanderConfig:
    """Commander 启动所需的最小 resolved 配置。"""

    ads: ADSSystemConfig | None
    devices: DevicesConfig
    point_tables: ResolvedPointTables
    connect_timeout: float = 10.0
    write_timeout: float = 5.0

    def points_for_device(self, device_id: str) -> list[PointConfig]:
        """返回设备绑定点表的浅拷贝。"""
        device = next(d for d in self.devices.devices if d.device_id == device_id)
        return list(self.point_tables.tables[device.point_table].points)


def _read_yaml(path: Path) -> dict[str, Any]:
    """安全读取 YAML 根映射。"""
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            data = cast(dict[str, Any], yaml.safe_load(handle))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"Empty configuration file: {path}")
    return data


def load_commander_config(config_dir: str | Path) -> CommanderConfig:
    """加载 Commander 所需配置并完成跨文件一致性校验。"""
    base = Path(config_dir)

    system_raw = _read_yaml(base / "system.yaml")
    models_raw = _read_yaml(base / "device_models.yaml")
    devices_raw = _read_yaml(base / "devices.yaml")
    points_raw = _read_yaml(base / "points.yaml")

    try:
        ads_raw = system_raw.get("ads")
        ads = ADSSystemConfig(**ads_raw) if isinstance(ads_raw, dict) else None
        runtime_raw = system_raw.get("runtime") or {}
        connect_timeout = float(runtime_raw.get("connect_timeout", 10.0))
        write_timeout = float(runtime_raw.get("write_timeout", 5.0))
        models = DeviceModelsConfig(**models_raw)
        instances = DeviceInstancesConfig(**devices_raw)
        point_tables = resolve_point_tables(
            PointTablesConfig(tables=points_raw.get("point_tables") or {})
        )
        devices = resolve_devices(instances, models)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid commander configuration: {exc}") from exc

    for model_id, model in models.device_models.items():
        table = point_tables.tables.get(model.point_table)
        if table is None:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table '{model.point_table}'"
            )
        if table.protocol != model.protocol:
            raise ConfigError(
                f"Device model '{model_id}' protocol '{model.protocol}' does not match "
                f"point table '{model.point_table}' protocol '{table.protocol}'"
            )


    if connect_timeout <= 0:
        raise ConfigError("runtime.connect_timeout must be > 0")
    if write_timeout <= 0:
        raise ConfigError("runtime.write_timeout must be > 0")

    return CommanderConfig(
        ads=ads,
        devices=devices,
        point_tables=point_tables,
        connect_timeout=connect_timeout,
        write_timeout=write_timeout,
    )
