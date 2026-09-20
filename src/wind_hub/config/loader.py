"""Configuration loader — YAML reading, per-model validation, cross-file checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub.config.point_table_resolver import resolve_point_tables
from wind_hub.config.reporting import load_reporting
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    DevicesConfig,
    PointConfig,
    PointTablesConfig,
    ReportingConfig,
    ResolvedPointTables,
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

    点表继承：``points.yaml`` 先按 Raw Schema 解析，再经
    ``point_table_resolver.resolve_point_tables`` 展开 ``extends`` /
    ``remove_points`` / override 为完整点集；以下全部交叉校验均作用于
    **resolved** 点表。

    Cross-file checks:
        - 设备引用的 ``point_table`` 必须存在；
        - 参与周期调度的设备：点位 ``group`` 必须被设备 ``polling`` 分组覆盖
          （无显式 polling 时仅允许 ``default``），且每个 polling 分组至少
          有一个点；
        - 点位级 ``sinks`` 与路由规则 ``targets`` 必须引用已定义的 sink；
        - ADS 设备点表的地址形式合法（symbol 单独合法；index_group 与
          index_offset 必须成对；三者不得全空）；
        - ADS ``sum`` 设备绑定表的全部点位必须配置 ``symbol``；
        - ADS ``sequential`` 设备只允许请求驱动的单次读取，不得配置
          ``polling``（不参与周期调度，分组覆盖校验同样跳过）。

    Raises:
        ConfigError: On any validation or consistency failure.
    """
    base = Path(config_dir)

    system = load_system(base / "system.yaml")
    devices = load_devices(base / "devices.yaml")
    # Raw 点表 → 继承展开 → Resolved 点表；后续全部校验与运行链路只接触
    # resolved 结果（ADS sum symbol、polling 覆盖、路由分组均按最终点集）。
    point_tables = resolve_point_tables(load_points(base / "points.yaml"))
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
    point_tables: ResolvedPointTables,
    sink_names: set[str],
) -> None:
    """校验单台设备的点表绑定、分组覆盖与协议相关约束。

    Raises:
        ConfigError: 点表缺失、点组未被 polling 覆盖、polling 分组无点、
            sink 引用未知、ADS 地址非法或 read_mode 组合非法。
    """
    table = point_tables.tables.get(device.point_table)
    if table is None:
        raise ConfigError(
            f"Device '{device.device_id}' references unknown point_table "
            f"'{device.point_table}' (available: {sorted(point_tables.tables)})"
        )

    # ADS sequential 只允许请求驱动的单次读取，不得配置周期 polling；
    # 其分组无调度含义，跳过覆盖校验。
    if not device.supports_scheduled_polling:
        if device.polling:
            raise ConfigError(
                f"Device '{device.device_id}': ADS read_mode='sequential' "
                f"must not configure polling (single reads only)"
            )
    else:
        # 点组必须被设备 polling 覆盖；无显式 polling 时仅 default 组（默认间隔）
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
        # 每个 polling 分组必须至少有一个点
        point_groups = {p.group for p in table.points}
        for g in device.polling:
            if g.group not in point_groups:
                raise ConfigError(
                    f"Device '{device.device_id}': polling group '{g.group}' "
                    f"has no points in table '{device.point_table}'"
                )

    # Per-point sinks override must reference valid sink names
    for p in table.points:
        for sn in p.sinks or []:
            if sn not in sink_names:
                raise ConfigError(
                    f"Point '{p.point_id}' references unknown sink '{sn}' "
                    f"(available: {sorted(sink_names)})"
                )

    if device.protocol == "ads":
        for p in table.points:
            _validate_ads_address(device.device_id, p)
            # sum 模式按 symbol 批量读，绑定表必须全部 symbol 寻址
            if device.read_mode == "sum":
                symbol = (p.address.model_extra or {}).get("symbol")
                if symbol is None:
                    raise ConfigError(
                        f"ADS point '{p.point_id}' (device '{device.device_id}'): "
                        f"read_mode='sum' requires 'symbol' on every point of "
                        f"table '{device.point_table}'"
                    )


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
