"""Configuration loader — YAML reading, per-model validation, cross-file checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub.config.device_resolver import resolve_devices
from wind_hub.config.point_table_resolver import resolve_point_tables
from wind_hub.config.reporting import load_reporting
from wind_hub.config.schema import (
    CollectionTaskConfig,
    Config,
    DeviceConfig,
    DeviceInstancesConfig,
    DeviceModelsConfig,
    DevicesConfig,
    PointConfig,
    PointTablesConfig,
    ReportingConfig,
    ResolvedPointTables,
    SystemConfig,
    TasksConfig,
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


def load_device_models(path: Path) -> DeviceModelsConfig:
    """Load and validate ``common/device_models.yaml``（设备类型 + 设备型号）。"""
    raw = _read_yaml(path)
    try:
        return DeviceModelsConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid device models config [{path}]: {exc}") from exc


def load_devices(path: Path) -> DeviceInstancesConfig:
    """Load and validate ``<site>/devices.yaml``（现场设备实例）。"""
    raw = _read_yaml(path)
    try:
        return DeviceInstancesConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid devices config [{path}]: {exc}") from exc


def load_points(path: Path) -> PointTablesConfig:
    """Load and validate ``points.yaml``（根键 ``point_tables``）。"""
    raw = _read_yaml(path)
    try:
        return PointTablesConfig(tables=raw.get("point_tables") or {})
    except Exception as exc:
        raise ConfigError(f"Invalid points config [{path}]: {exc}") from exc


def load_tasks(path: Path) -> TasksConfig:
    """Load and validate ``tasks.yaml``。"""
    raw = _read_yaml(path)
    try:
        return TasksConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid tasks config [{path}]: {exc}") from exc


def load_config(config_dir: str | Path, common_dir: str | Path | None = None) -> Config:
    """Load all configuration files, validate each, run cross-file
    consistency checks, and return an aggregate ``Config``.

    配置目录组织为「公共产品定义 + 单现场实例配置」：

    - ``config_dir`` — 现场配置目录（``<site>/``），含 ``system.yaml``、
      ``devices.yaml``、``tasks.yaml``（``reporting.yaml`` 可选）；
    - ``common_dir`` — 公共定义目录，含 ``device_models.yaml``、
      ``points.yaml``；缺省为 ``config_dir`` 的同级 ``common/``
      （即 ``config_dir.parent / 'common'``）。

    加载顺序：

    1. ``<site>/system.yaml``（含 site 现场身份）；
    2. ``common/device_models.yaml``（设备类型 + 设备型号）；
    3. ``common/points.yaml`` → 点表继承展开（extends / remove_points /
       override，见 ``point_table_resolver``）；
    4. ``<site>/devices.yaml``（设备实例）；
    5. ``device_resolver.resolve_devices`` — 实例 + 型号合并为 resolved
       运行时 ``DeviceConfig``；
    6. ``<site>/tasks.yaml``；
    7. ``<site>/reporting.yaml``（可选）；
    8. 跨文件校验（见下）；
    9. 聚合为 ``Config``。

    Cross-file checks（全部作用于 resolved 模型）:

        - 型号引用的 ``point_table`` 必须存在；
        - 设备（resolved）绑定点表的协议约束——ADS 地址形式合法
          （symbol 单独合法；index_group 与 index_offset 必须成对；三者
          不得全空），ADS ``sum`` 设备绑定表的全部点位必须配置 ``symbol``；
        - Task 的 ``device`` 必须存在；``device_group`` 至少匹配一台 enabled
          设备；
        - Task 的 ``point_group`` 必须在其命中的每台 enabled 设备绑定点表
          中存在；
        - Task 的 ``targets`` 必须引用已定义的 sink；
        - 任何 Task 不得命中 ADS ``read_mode='sequential'`` 的设备（该模式
          只允许请求驱动的单次读取，不参与周期采集）。

    Raises:
        ConfigError: On any validation or consistency failure.
    """
    base = Path(config_dir)
    common = Path(common_dir) if common_dir is not None else base.parent / "common"

    system = load_system(base / "system.yaml")
    device_models = load_device_models(common / "device_models.yaml")
    # Raw 点表 → 继承展开 → Resolved 点表；后续全部校验与运行链路只接触
    # resolved 结果（ADS sum symbol、Task point_group 覆盖均按最终点集）。
    point_tables = resolve_point_tables(load_points(common / "points.yaml"))
    # 设备实例 + 型号 → resolved 运行时 DeviceConfig（Runtime 不再回查
    # 原始 DeviceModel）。
    devices = resolve_devices(load_devices(base / "devices.yaml"), device_models)
    tasks = load_tasks(base / "tasks.yaml")

    # Optional IEC104 slave proxy config — absent means no proxy.
    reporting: ReportingConfig | None = None
    reporting_path = base / "reporting.yaml"
    if reporting_path.is_file():
        reporting = load_reporting(reporting_path)

    _validate_model_point_tables(device_models, point_tables)
    for device in devices.devices:
        _validate_device_binding(device, point_tables)

    sink_names = {s.name for s in system.sinks}
    for task in tasks.tasks:
        _validate_task_targets(task, devices, point_tables, sink_names)

    return Config(
        system=system,
        device_types=device_models.device_types,
        device_models=device_models.device_models,
        devices=devices,
        point_tables=point_tables,
        tasks=tasks,
        reporting=reporting,
    )


def _validate_model_point_tables(
    device_models: DeviceModelsConfig,
    point_tables: ResolvedPointTables,
) -> None:
    """校验全部型号引用的点表存在（未被实例引用的型号同样校验——
    公共定义库自身必须自洽）。"""
    for model_id, m in device_models.device_models.items():
        if m.point_table not in point_tables.tables:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table "
                f"'{m.point_table}' (available: {sorted(point_tables.tables)})"
            )


def _validate_task_targets(
    task: CollectionTaskConfig,
    devices: DevicesConfig,
    point_tables: ResolvedPointTables,
    sink_names: set[str],
) -> None:
    """校验单个采集 Task 的跨文件引用。

    - ``device`` 必须存在；``device_group`` 至少匹配一台 enabled 设备；
    - 命中的 enabled 设备必须全部支持周期采集（ADS ``sequential`` 报错）；
    - ``point_group`` 必须在每台命中 enabled 设备的绑定点表中存在；
    - 每个 target sink 必须已定义。

    Raises:
        ConfigError: 任一引用缺失或组合非法。
    """
    for target in task.targets:
        if target.sink not in sink_names:
            raise ConfigError(
                f"Task '{task.task_id}' targets unknown sink "
                f"'{target.sink}' (available: {sorted(sink_names)})"
            )

    if task.device is not None:
        device = next((d for d in devices.devices if d.device_id == task.device), None)
        if device is None:
            raise ConfigError(f"Task '{task.task_id}' references unknown device '{task.device}'")
        matched = [device] if device.enabled else []
    else:
        matched = [d for d in devices.devices if d.enabled and d.device_group == task.device_group]
        if not any(d.device_group == task.device_group for d in devices.devices):
            raise ConfigError(
                f"Task '{task.task_id}': device_group '{task.device_group}' " "matches no device"
            )

    unsupported = [d.device_id for d in matched if not d.supports_scheduled_collection]
    if unsupported:
        raise ConfigError(
            f"Task '{task.task_id}': devices {unsupported} do not support "
            "scheduled collection (ADS read_mode='sequential' is single-read only)"
        )

    # interval 是否必填按命中设备的协议采集能力判定：主动轮询（Modbus、
    # ADS Sum）与 ADS 订阅（notification cycle_time）都需要节拍；纯
    # IEC104 订阅由远端决定数据到达时机，不要求 interval。混合命中时
    # （如 device_group 同时含 IEC104 与 Modbus）仍必须配置。
    if task.interval is None:
        requiring = [d.device_id for d in matched if _device_requires_interval(d)]
        if requiring:
            raise ConfigError(
                f"Task '{task.task_id}': interval is required — devices {requiring} "
                "collect actively (poll) or via ADS notification cycle_time; "
                "only pure IEC104-subscription tasks may omit interval"
            )

    missing = [
        d.device_id
        for d in matched
        if task.point_group
        not in {g for p in point_tables.tables[d.point_table].points for g in p.point_groups}
    ]
    if missing:
        raise ConfigError(
            f"Task '{task.task_id}': point_group '{task.point_group}' does not "
            f"exist in the point tables of devices {missing}"
        )


def _device_requires_interval(device: DeviceConfig) -> bool:
    """设备的采集机制是否需要 Task 提供节拍（interval）。

    - IEC104：订阅式（spontaneous / periodic），不需要；
    - ADS 且 ``subscribe_enabled``：订阅式，但 interval 用作 notification
      ``cycle_time``——需要；
    - 其余（Modbus、ADS Sum 主动轮询）：需要。
    """
    return device.protocol != "iec104"


def _validate_device_binding(
    device: DeviceConfig,
    point_tables: ResolvedPointTables,
) -> None:
    """校验单台设备的点表绑定与协议相关约束。

    Raises:
        ConfigError: 点表缺失、ADS 地址非法或 read_mode 组合非法。
    """
    table = point_tables.tables.get(device.point_table)
    if table is None:
        raise ConfigError(
            f"Device '{device.device_id}' references unknown point_table "
            f"'{device.point_table}' (available: {sorted(point_tables.tables)})"
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
