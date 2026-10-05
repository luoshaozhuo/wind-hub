"""Wind Hub YAML 配置加载编排。

加载器负责把一个自包含配置目录转换为 resolved Config：

    read（YAML 安全读取）→ parse（单文件 schema 校验）→ resolve（继承展开 /
    型号合并 / Sink 引用解析）→ validate（跨文件一致性，见 ``config/validation``）
    → build Config

它不创建 Device/Protocol/Sink，也不执行网络 I/O。

YAML 原始值使用 dict[str, Any] 是安全反序列化后的动态输入边界；进入各 Pydantic
schema 后收敛为强类型配置对象。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub_core.config.model.config import Config
from wind_hub_core.config.model.device import DeviceInstancesConfig, DeviceModelsConfig
from wind_hub_core.config.model.point import PointTablesConfig
from wind_hub_core.config.model.sink import SinksConfig
from wind_hub_core.config.model.system import SystemConfig
from wind_hub_core.config.model.task import TasksConfig
from wind_hub_core.config.model.unit import UnitsConfig
from wind_hub_core.config.resolver.device import resolve_devices
from wind_hub_core.config.resolver.point_table import resolve_point_tables
from wind_hub_core.config.resolver.sink import resolve_sinks
from wind_hub_core.config.validation.addresses import validate_table_addresses
from wind_hub_core.config.validation.references import (
    validate_device_binding,
    validate_model_point_tables,
    validate_point_units,
)
from wind_hub_core.config.validation.task import validate_task_targets
from wind_hub_core.model.errors import ConfigError


def _read_yaml(path: Path) -> dict[str, Any]:
    """安全读取单个 YAML 文件。

    Args:
        path: YAML 文件路径。

    Returns:
        YAML 根映射。Any 仅存在于 schema 校验前的动态配置边界。

    Raises:
        ConfigError: 文件缺失、为空或 YAML 语法非法。
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
    """加载并校验 system.yaml。

    Args:
        path: system.yaml 文件路径。

    Returns:
        校验完成的 SystemConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    raw = _read_yaml(path)
    try:
        return SystemConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid system config [{path}]: {exc}") from exc


def load_sinks(path: Path) -> SinksConfig:
    """加载并校验 sinks.yaml 统一 Sink 外部接口契约。"""
    raw = _read_yaml(path)
    try:
        return SinksConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid sinks config [{path}]: {exc}") from exc


def load_units(path: Path) -> UnitsConfig:
    """加载并校验 units.yaml 单位定义集。

    Args:
        path: units.yaml 文件路径。

    Returns:
        校验完成的 UnitsConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    raw = _read_yaml(path)
    try:
        return UnitsConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid units config [{path}]: {exc}") from exc


def load_device_models(path: Path) -> DeviceModelsConfig:
    """加载并校验 device_models.yaml。

    Args:
        path: device_models.yaml 文件路径。

    Returns:
        校验完成的 DeviceModelsConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    raw = _read_yaml(path)
    try:
        return DeviceModelsConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid device models config [{path}]: {exc}") from exc


def load_devices(path: Path) -> DeviceInstancesConfig:
    """加载并校验 devices.yaml 现场设备实例。

    Args:
        path: devices.yaml 文件路径。

    Returns:
        校验完成的 DeviceInstancesConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    raw = _read_yaml(path)
    try:
        return DeviceInstancesConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid devices config [{path}]: {exc}") from exc


def load_points(path: Path) -> PointTablesConfig:
    """加载并校验 points.yaml，并读取 point_tables 根键。

    Args:
        path: points.yaml 文件路径。

    Returns:
        校验完成的 PointTablesConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    raw = _read_yaml(path)
    try:
        return PointTablesConfig(tables=raw.get("point_tables") or {})
    except Exception as exc:
        raise ConfigError(f"Invalid points config [{path}]: {exc}") from exc


def load_tasks(path: Path) -> TasksConfig:
    """加载并校验 tasks.yaml。

    Args:
        path: tasks.yaml 文件路径。

    Returns:
        校验完成的 TasksConfig。

    Raises:
        ConfigError: 文件缺失、YAML 非法或 schema 校验失败。
    """
    raw = _read_yaml(path)
    try:
        return TasksConfig(**raw)
    except Exception as exc:
        raise ConfigError(f"Invalid tasks config [{path}]: {exc}") from exc


def load_config(config_dir: str | Path) -> Config:
    """加载一个自包含配置目录并返回 resolved Config。

    加载顺序：
    1. system.yaml、units.yaml、device_models.yaml；
    2. points.yaml 并展开点表继承；
    3. devices.yaml 与型号默认值合并为运行时 DeviceConfig；
    4. tasks.yaml；
    5. 执行跨文件引用、协议地址、point_group、Sink target 等一致性校验。

    Args:
        config_dir: 完整现场配置目录。

    Returns:
        Runtime 可直接消费的 resolved Config。

    Raises:
        ConfigError: 任一文件、schema、引用关系或协议组合非法。
    """
    base = Path(config_dir)

    system = load_system(base / "system.yaml")
    raw_sinks = load_sinks(base / "sinks.yaml")
    units = load_units(base / "units.yaml")
    device_models = load_device_models(base / "device_models.yaml")
    # Raw 点表 → 继承展开 → Resolved 点表；后续全部校验与运行链路只接触
    # resolved 结果（ADS sum symbol、Task point_group 覆盖均按最终点集）。
    point_tables = resolve_point_tables(load_points(base / "points.yaml"))
    # 设备实例 + 型号 → resolved 运行时 DeviceConfig（Runtime 不再回查
    # 原始 DeviceModel）。
    devices = resolve_devices(load_devices(base / "devices.yaml"), device_models)
    tasks = load_tasks(base / "tasks.yaml")

    validate_model_point_tables(device_models, point_tables)
    validate_point_units(point_tables, units)
    sinks = resolve_sinks(raw_sinks, devices, point_tables, units)
    validate_table_addresses(point_tables)
    for device in devices.devices:
        validate_device_binding(device, point_tables)

    sink_names = {s.name for s in sinks.sinks if s.enabled}
    for task in tasks.tasks:
        validate_task_targets(task, devices, point_tables, sink_names)

    return Config(
        system=system,
        sinks=sinks,
        units=units,
        device_types=device_models.device_types,
        device_models=device_models.device_models,
        devices=devices,
        point_tables=point_tables,
        tasks=tasks,
    )


__all__ = [
    "load_system",
    "load_sinks",
    "load_units",
    "load_device_models",
    "load_devices",
    "load_points",
    "load_tasks",
    "load_config",
]
