"""Collector YAML 配置加载与跨文件一致性校验。

加载器负责把一个自包含配置目录转换为 resolved Config，并在 Runtime 装配前完成
schema、引用关系、协议地址和 Task 组合约束校验。它不创建 Device/Protocol/Sink，
也不执行网络 I/O。

YAML 原始值使用 dict[str, Any] 是安全反序列化后的动态输入边界；进入各 Pydantic
schema 后收敛为强类型配置对象。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from wind_hub_core.config.device_resolver import resolve_devices
from wind_hub_core.config.point_table_resolver import resolve_point_tables
from wind_hub.config.reporting import load_reporting
from wind_hub.config.schema import (
    CollectionTaskConfig,
    Config,
    ReportingConfig,
    SystemConfig,
    TasksConfig,
)
from wind_hub_core.config.schema import (
    DeviceConfig,
    DeviceInstancesConfig,
    DeviceModelsConfig,
    DevicesConfig,
    PointConfig,
    PointTablesConfig,
    ResolvedPointTables,
    UnitsConfig,
)
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
    5. 可选 reporting.yaml；
    6. 执行跨文件引用、协议地址、point_group、Sink target 等一致性校验。

    Args:
        config_dir: 完整现场配置目录。

    Returns:
        Runtime 可直接消费的 resolved Config。

    Raises:
        ConfigError: 任一文件、schema、引用关系或协议组合非法。
    """
    base = Path(config_dir)

    system = load_system(base / "system.yaml")
    units = load_units(base / "units.yaml")
    device_models = load_device_models(base / "device_models.yaml")
    # Raw 点表 → 继承展开 → Resolved 点表；后续全部校验与运行链路只接触
    # resolved 结果（ADS sum symbol、Task point_group 覆盖均按最终点集）。
    point_tables = resolve_point_tables(load_points(base / "points.yaml"))
    # 设备实例 + 型号 → resolved 运行时 DeviceConfig（Runtime 不再回查
    # 原始 DeviceModel）。
    devices = resolve_devices(load_devices(base / "devices.yaml"), device_models)
    tasks = load_tasks(base / "tasks.yaml")

    # reporting.yaml 可选；不存在即不启用 IEC104 slave proxy。
    reporting: ReportingConfig | None = None
    reporting_path = base / "reporting.yaml"
    if reporting_path.is_file():
        reporting = load_reporting(reporting_path)

    _validate_model_point_tables(device_models, point_tables)
    _validate_point_units(point_tables, units)
    _validate_table_addresses(point_tables)
    for device in devices.devices:
        _validate_device_binding(device, point_tables)

    sink_names = {s.name for s in system.sinks}
    for task in tasks.tasks:
        _validate_task_targets(task, devices, point_tables, sink_names)

    return Config(
        system=system,
        units=units,
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
    """校验全部型号引用的点表存在、且型号协议与点表协议一致（未被实例
    引用的型号同样校验——型号定义自身必须自洽）。"""
    for model_id, m in device_models.device_models.items():
        table = point_tables.tables.get(m.point_table)
        if table is None:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table "
                f"'{m.point_table}' (available: {sorted(point_tables.tables)})"
            )
        if m.protocol != table.protocol:
            raise ConfigError(
                f"Device model '{model_id}': protocol '{m.protocol}' does not match "
                f"point table '{m.point_table}' protocol '{table.protocol}'"
            )


def _validate_point_units(
    point_tables: ResolvedPointTables,
    units: UnitsConfig,
) -> None:
    """校验继承展开后全部点的 ``unit`` 是已定义的 unit ID。"""
    for table_name, table in point_tables.tables.items():
        for p in table.points:
            if p.unit not in units.units:
                raise ConfigError(
                    f"Point '{p.point_id}' (table '{table_name}') references "
                    f"unknown unit '{p.unit}' (available: {sorted(units.units)})"
                )


def _validate_table_addresses(point_tables: ResolvedPointTables) -> None:
    """按点表 protocol 校验全部 resolved 点的地址形式（配置期失败，不等
    运行时读失败）。"""
    for table_name, table in point_tables.tables.items():
        for p in table.points:
            if table.protocol == "ads":
                _validate_ads_address(table_name, p)
            elif table.protocol == "modbus":
                _validate_modbus_address(table_name, p)
            elif table.protocol == "iec104":
                _validate_iec104_address(table_name, p)


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
    """校验单台设备的点表绑定与 read_mode 组合约束。

    点地址形式已在 ``_validate_table_addresses`` 按点表 protocol 统一
    校验；此处只保留设备级约束（ADS ``sum`` 的全 symbol 要求）。

    Raises:
        ConfigError: 点表缺失或 read_mode 组合非法。
    """
    table = point_tables.tables.get(device.point_table)
    if table is None:
        raise ConfigError(
            f"Device '{device.device_id}' references unknown point_table "
            f"'{device.point_table}' (available: {sorted(point_tables.tables)})"
        )

    if device.protocol == "ads" and device.read_mode == "sum":
        for p in table.points:
            # sum 模式按 symbol 批量读，绑定表必须全部 symbol 寻址
            symbol = (p.address.model_extra or {}).get("symbol")
            if symbol is None:
                raise ConfigError(
                    f"ADS point '{p.point_id}' (device '{device.device_id}'): "
                    f"read_mode='sum' requires 'symbol' on every point of "
                    f"table '{device.point_table}'"
                )


def _validate_ads_address(table: str, point: PointConfig) -> None:
    """校验 ADS 点位地址形式。

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
            f"ADS point '{point.point_id}' (table '{table}'): "
            f"'index_group' and 'index_offset' must be configured together"
        )
    if symbol is None and index_group is None:
        raise ConfigError(
            f"ADS point '{point.point_id}' (table '{table}'): address must "
            f"define 'symbol' or 'index_group' + 'index_offset'"
        )


# Modbus register_type 合法取值（与 ``adapter/outbound/protocol/modbus/
# mapping.py`` 的驱动解析保持一致；loader 不反向依赖 adapter 层）。
_MODBUS_REGISTER_TYPES = frozenset(
    {
        "coil",
        "discrete_input",
        "discrete",
        "input",
        "input_register",
        "holding",
        "holding_register",
    }
)


def _validate_modbus_address(table: str, point: PointConfig) -> None:
    """校验 Modbus 点位地址形式（与驱动 ``parse_point`` 的必填字段一致）。

    - ``register_type``（extra 字段）或 ``type``（address 模型字段）必须
      是 coil / discrete_input / holding / input 之一（含别名）；
    - ``address`` 必须是非负整数（0-based 寄存器/线圈偏移）。

    Raises:
        ConfigError: register_type 缺失/非法，或 address 缺失/非法。
    """
    extra = point.address.model_extra or {}
    register_type = extra.get("register_type")
    if register_type is None:
        register_type = point.address.type
    if register_type is None or str(register_type).lower() not in _MODBUS_REGISTER_TYPES:
        raise ConfigError(
            f"Modbus point '{point.point_id}' (table '{table}'): missing or invalid "
            f"register_type '{register_type}' (expected coil/discrete_input/holding/input)"
        )
    address = extra.get("address")
    if isinstance(address, bool) or not isinstance(address, int) or address < 0:
        raise ConfigError(
            f"Modbus point '{point.point_id}' (table '{table}'): address must be "
            f"a non-negative integer, got {address!r}"
        )


def _validate_iec104_address(table: str, point: PointConfig) -> None:
    """校验 IEC104 点位地址形式——``ioa`` 必须是 [0, 0xFFFFFF] 的整数。

    Raises:
        ConfigError: ioa 缺失或越界。
    """
    extra = point.address.model_extra or {}
    ioa = extra.get("ioa")
    if isinstance(ioa, bool) or not isinstance(ioa, int) or not 0 <= ioa <= 0xFFFFFF:
        raise ConfigError(
            f"IEC104 point '{point.point_id}' (table '{table}'): ioa must be "
            f"an integer in [0, 0xFFFFFF], got {ioa!r}"
        )
