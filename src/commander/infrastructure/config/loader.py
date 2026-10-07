"""Commander 现场配置加载入口。

读取同一现场配置目录中的 Commander 子集（system / device_models /
devices / points / units），完成 Raw 解析、点表继承展开、Core 快照组装
与跨文件一致性校验，产出 CommanderConfig。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from core.application import ConfigError

from ...application.config import ADSLocalIdentity, CommanderConfig
from .point_tables import resolve_point_tables
from .raw import (
    ADSSystemRaw,
    DeviceInstancesFile,
    DeviceModelsFile,
    PointTablesFile,
    UnitsFile,
)
from .snapshot import build_core_snapshot


def load_commander_config(config_dir: str | Path) -> CommanderConfig:
    """加载 Commander 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    base = Path(config_dir)

    system_raw = _read_yaml(base / "system.yaml")
    models_raw = _read_yaml(base / "device_models.yaml")
    devices_raw = _read_yaml(base / "devices.yaml")
    points_raw = _read_yaml(base / "points.yaml")
    units_raw = _read_yaml(base / "units.yaml")

    try:
        ads_raw = system_raw.get("ads")
        ads = ADSSystemRaw(**ads_raw) if isinstance(ads_raw, dict) else None
        runtime_raw = system_raw.get("runtime") or {}
        if not isinstance(runtime_raw, dict):
            raise ConfigError("system.yaml 'runtime' must be a mapping")
        connect_timeout = float(runtime_raw.get("connect_timeout", 10.0))
        write_timeout = float(runtime_raw.get("write_timeout", 5.0))

        models_file = DeviceModelsFile(**models_raw)
        instances_file = DeviceInstancesFile(**devices_raw)
        tables_file = PointTablesFile(**points_raw)
        units_file = UnitsFile(**units_raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid commander configuration: {exc}") from exc

    tables = resolve_point_tables(tables_file.point_tables)
    snapshot, point_meta, disabled = build_core_snapshot(
        models_file=models_file,
        instances_file=instances_file,
        tables=tables,
        units_file=units_file,
    )

    if connect_timeout <= 0:
        raise ConfigError("runtime.connect_timeout must be > 0")
    if write_timeout <= 0:
        raise ConfigError("runtime.write_timeout must be > 0")

    return CommanderConfig(
        core=snapshot,
        ads_local=(
            ADSLocalIdentity(
                local_ams_net_id=ads.local_ams_net_id,
                local_ip=ads.local_ip,
            )
            if ads is not None
            else None
        ),
        connect_timeout=connect_timeout,
        write_timeout=write_timeout,
        point_meta=point_meta,
        disabled_devices=disabled,
    )


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
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a mapping: {path}")
    return data


__all__ = ["load_commander_config"]
