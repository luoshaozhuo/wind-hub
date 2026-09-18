"""Configuration loader — YAML reading, per-model validation, cross-file checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub.config.reporting import load_reporting
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    DevicesConfig,
    PointConfig,
    PointTablesConfig,
    ReportingConfig,
    RoutingConfig,
    SystemConfig,
)
from wind_hub.domain.model.errors import ConfigError


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a single YAML file.

    Raises:
        ConfigError: If the file is missing or contains invalid YAML.
    """
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = cast(dict[str, Any], yaml.safe_load(fh))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"Empty configuration file: {path}")
    return data


def load_system(path: Path) -> SystemConfig:
    """Load and validate ``system.yaml``."""
    raw = _read_yaml(path)
    try:
        return SystemConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid system config [{path}]: {exc}") from exc


def load_devices(path: Path) -> DevicesConfig:
    """Load and validate ``devices.yaml``."""
    raw = _read_yaml(path)
    try:
        return DevicesConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid devices config [{path}]: {exc}") from exc


def load_points(path: Path) -> PointTablesConfig:
    """Load and validate ``points.yaml``（根键 ``point_tables``）。"""
    raw = _read_yaml(path)
    try:
        return PointTablesConfig(tables=raw.get("point_tables") or {})
    except Exception as exc:
        raise ConfigError(f"Invalid points config [{path}]: {exc}") from exc


def load_routing(path: Path) -> RoutingConfig:
    """Load and validate ``routing.yaml``."""
    raw = _read_yaml(path)
    try:
        return RoutingConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid routing config [{path}]: {exc}") from exc


def load_config(config_dir: str | Path) -> Config:
    """Load all four configuration files from a directory, validate each,
    run cross-file consistency checks, and return an aggregate ``Config``.

    Expected files:
        ``system.yaml``, ``devices.yaml``, ``points.yaml``, ``routing.yaml``

    Cross-file checks:
        - 设备引用的 ``point_table`` 必须存在；
        - 点位 ``group`` 必须被设备 ``polling`` 分组覆盖（无显式 polling 时
          仅允许 ``default``）；
        - 点位级 ``sinks`` 与路由规则 ``targets`` 必须引用已定义的 sink；
        - ADS 设备点表的地址形式合法（symbol 单独合法；index_group 与
          index_offset 必须成对；三者不得全空）。

    ``read_mode``（如何读：sequential/sum）与 ``mode``（何时读：poll/
    subscribe）是正交概念，本层不做组合限制——sequential 周期轮询合法
    （逐点读性能较低，属于部署取舍而非配置错误）。

    Raises:
        ConfigError: On any validation or consistency failure.
    """
    base = Path(config_dir)

    system = load_system(base / "system.yaml")
    devices = load_devices(base / "devices.yaml")
    point_tables = load_points(base / "points.yaml")
    routing = load_routing(base / "routing.yaml")

    # Optional IEC104 slave proxy config — absent means no proxy.
    reporting: ReportingConfig | None = None
    reporting_path = base / "reporting.yaml"
    if reporting_path.is_file():
        reporting = load_reporting(reporting_path)

    sink_names = {s.name for s in system.sinks}

    for device in devices.devices:
        _validate_device_binding(device, point_tables, sink_names)

    # Cross-file: routing rule targets must reference valid sink names
    for rule in routing.rules:
        for target in rule.targets:
            if target.sink not in sink_names:
                raise ConfigError(
                    f"Routing rule '{rule.name}' targets unknown sink "
                    f"'{target.sink}' (available: {sorted(sink_names)})"
                )

    return Config(
        system=system,
        devices=devices,
        point_tables=point_tables,
        routing=routing,
        reporting=reporting,
    )


def _validate_device_binding(
    device: DeviceConfig,
    point_tables: PointTablesConfig,
    sink_names: set[str],
) -> None:
    """校验单台设备的点表绑定、分组覆盖与协议相关约束。

    Raises:
        ConfigError: 点表缺失、点组未被 polling 覆盖、sink 引用未知或
            ADS 地址非法。
    """
    table = point_tables.tables.get(device.point_table)
    if table is None:
        raise ConfigError(
            f"Device '{device.device_id}' references unknown point_table "
            f"'{device.point_table}' (available: {sorted(point_tables.tables)})"
        )

    # 点组必须被设备 polling 覆盖；无显式 polling 时仅 default 组（用默认间隔）
    allowed_groups = (
        {g.group for g in device.polling} if device.polling else {"default"}
    )
    for p in table.points:
        if p.group not in allowed_groups:
            raise ConfigError(
                f"Device '{device.device_id}': point '{p.point_id}' uses group "
                f"'{p.group}' not covered by device polling groups "
                f"{sorted(allowed_groups)}"
            )
        # Per-point sinks override must reference valid sink names
        for sn in p.sinks or []:
            if sn not in sink_names:
                raise ConfigError(
                    f"Point '{p.point_id}' references unknown sink '{sn}' "
                    f"(available: {sorted(sink_names)})"
                )

    if device.protocol == "ads":
        for p in table.points:
            _validate_ads_address(device.device_id, p)


def _validate_ads_address(device_id: str, point: PointConfig) -> None:
    """校验 ADS 点位地址形式（配置期失败，不等运行时读失败）。

    合法形式：
      1. 仅 ``symbol``——Symbol 寻址（推荐）；
      2. ``index_group`` + ``index_offset`` 成对——兼容寻址；
      3. 两者同时存在——允许，实际读写以 symbol 优先。

    Raises:
        ConfigError: index 字段不成对，或三者全空。
    """
    extra = point.address.model_extra or {}
    symbol = extra.get("symbol")
    index_group = extra.get("index_group")
    index_offset = extra.get("index_offset")

    if (index_group is None) != (index_offset is None):
        raise ConfigError(
            f"ADS point '{point.point_id}' (device '{device_id}'): "
            f"'index_group' and 'index_offset' must be configured together"
        )
    if symbol is None and index_group is None:
        raise ConfigError(
            f"ADS point '{point.point_id}' (device '{device_id}'): address must "
            f"define 'symbol' or 'index_group' + 'index_offset'"
        )
