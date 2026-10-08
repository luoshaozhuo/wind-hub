"""Collector Raw 配置 → CoreConfigSnapshot 组装。

把 device_models / devices / points / units 的 Raw 模型合并为共享 Core
的不可变配置快照，同时产出 Collector 进程级附属配置：

- ``point_meta``：点位的 variable_name / point_groups（采集选点分组与
  展示元数据，不属于协议 Point.ext，也不属于共享 Domain 点定义）；
- ``disabled_devices``：``enabled: false`` 的设备不进快照，记入本集合；
- ``ads_subscribe_devices``：ADS 设备 endpoint extensions 中的
  ``subscribe_enabled: true`` 被提取为本集合——新 Core 的 ADS Driver 严格
  拒绝未知 option，订阅开关不再进入 device_options。

映射规则（与旧系统行为对齐）：

- 端点合并：``port`` 取实例 endpoint.port，缺省取型号
  ``connection_defaults.port``，两者皆无是配置错误；其余
  connection_defaults 键与实例 endpoint.extensions 合并（实例优先）成为
  协议 device_options；ADS 额外注入型号 ``read_mode``（缺省 ``sum``）；
- 单位：``point.unit`` 必须是 Core 内置 canonical unit code（引用
  units.yaml 中不存在的 ID、或 ID 非内置单位均为配置错误）；
- PointAccess 推导：modbus 只读寄存器（discrete_input/input）→ READ，
  其余 → READ_WRITE；ads → READ_WRITE；iec104 type_id 以 ``C_`` 开头
  → READ_WRITE，否则 → READ；
- BusinessPoint 合成：默认 ``business_point_id == point_id``；同名点跨表
  且 (data_type, unit) 冲突时改用 ``{table}:{point_id}``。
"""

from __future__ import annotations

from typing import Any

from core.application import ConfigError
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionEndpoint,
    CoreConfigSnapshot,
    DataType,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    ProtocolOptionValue,
    validate_core_config,
)
from core.domain.unit import UNIT_CATALOG, Unit, UnitCode

from ...application.config import PointMeta
from .point_tables import ResolvedTable
from .raw import (
    DeviceInstancesFile,
    DeviceModelRaw,
    DeviceModelsFile,
    PointConfigRaw,
    UnitsFile,
)

_MODBUS_READ_ONLY = frozenset({"discrete_input", "discrete", "input", "input_register"})


def build_core_snapshot(
    *,
    models_file: DeviceModelsFile,
    instances_file: DeviceInstancesFile,
    tables: dict[str, ResolvedTable],
    units_file: UnitsFile,
) -> tuple[
    CoreConfigSnapshot,
    dict[PointTableId, dict[str, PointMeta]],
    frozenset[DeviceId],
    frozenset[DeviceId],
]:
    """合并 Raw 配置为 Core 快照 + 进程级附属配置。

    Returns:
        (CoreConfigSnapshot, point_meta, disabled_devices, ads_subscribe_devices)。
        point_meta 为 ``{点表: {point_id: PointMeta}}``。

    Raises:
        ConfigError: 任何引用缺失、协议不一致或领域不变量违反。
    """
    device_types = {
        DeviceTypeId(type_id): DeviceType(
            device_type_id=DeviceTypeId(type_id),
            name=raw.name or type_id,
        )
        for type_id, raw in models_file.device_types.items()
    }

    business_points: dict[BusinessPointId, BusinessPoint] = {}
    point_tables: dict[PointTableId, PointTable] = {}
    point_meta: dict[PointTableId, dict[str, PointMeta]] = {}
    for table_name, resolved in tables.items():
        table_id = PointTableId(table_name)
        points: dict[str, Point] = {}
        meta: dict[str, PointMeta] = {}
        for raw_point in resolved.points.values():
            point = _build_point(
                table_id,
                resolved.protocol,
                raw_point,
                units_file,
                business_points,
            )
            points[point.point_id] = point
            meta[point.point_id] = PointMeta(
                variable_name=raw_point.variable_name,
                point_groups=tuple(raw_point.point_groups),
            )
        point_tables[table_id] = PointTable(
            point_table_id=table_id,
            protocol=Protocol(resolved.protocol),
            points=points,
        )
        point_meta[table_id] = meta

    device_models: dict[DeviceModelId, DeviceModel] = {}
    for model_id, raw_model in models_file.device_models.items():
        table = point_tables.get(PointTableId(raw_model.point_table))
        if table is None:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table "
                f"'{raw_model.point_table}'"
            )
        if table.protocol.name != raw_model.protocol:
            raise ConfigError(
                f"Device model '{model_id}' protocol '{raw_model.protocol}' "
                f"does not match point table '{raw_model.point_table}' "
                f"protocol '{table.protocol.name}'"
            )
        device_models[DeviceModelId(model_id)] = DeviceModel(
            device_model_id=DeviceModelId(model_id),
            device_type_id=DeviceTypeId(raw_model.device_type),
            point_table_id=PointTableId(raw_model.point_table),
            name=raw_model.model,
            manufacturer=raw_model.manufacturer,
        )

    devices: dict[DeviceId, Device] = {}
    device_options: dict[DeviceId, dict[str, ProtocolOptionValue]] = {}
    group_names: set[str] = set()
    disabled: set[DeviceId] = set()
    ads_subscribe: set[DeviceId] = set()
    for instance in instances_file.devices:
        device_id = DeviceId(instance.device_id)
        if not instance.enabled:
            disabled.add(device_id)
            continue
        raw_model = _lookup_model(instance.device_id, instance.model, models_file)
        model_id = DeviceModelId(instance.model)
        endpoint, options = _merge_endpoint(
            instance.device_id,
            host=instance.endpoint.host,
            port=instance.endpoint.port,
            extensions=instance.endpoint.extensions,
            raw_model=raw_model,
        )
        group_ids: tuple[DeviceGroupId, ...] = ()
        if instance.device_group is not None:
            group_names.add(instance.device_group)
            group_ids = (DeviceGroupId(instance.device_group),)
        devices[device_id] = Device(
            device_id=device_id,
            device_model_id=model_id,
            endpoint=endpoint,
            device_group_ids=group_ids,
        )
        # ADS 订阅开关是 Collector 采集模式选择，不是 Driver 连接参数：
        # 从 device_options 剥离（Core ADS Driver 严格拒绝未知 option），
        # 记入 ads_subscribe_devices。
        if raw_model.protocol == "ads" and options.pop("subscribe_enabled", False):
            ads_subscribe.add(device_id)
        device_options[device_id] = options

    device_groups = {
        DeviceGroupId(name): DeviceGroup(
            device_group_id=DeviceGroupId(name),
            name=name,
        )
        for name in sorted(group_names)
    }

    try:
        snapshot = CoreConfigSnapshot(
            device_types=device_types,
            device_models=device_models,
            device_groups=device_groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            device_options=device_options,
        )
        validate_core_config(snapshot)
    except ValueError as exc:
        raise ConfigError(f"invalid core configuration: {exc}") from exc

    return (
        snapshot,
        point_meta,
        frozenset(disabled),
        frozenset(ads_subscribe),
    )


def _lookup_model(
    device_id: str,
    model_id: str,
    models_file: DeviceModelsFile,
) -> DeviceModelRaw:
    model = models_file.device_models.get(model_id)
    if model is None:
        raise ConfigError(
            f"Device '{device_id}' references unknown model '{model_id}' "
            f"(available: {sorted(models_file.device_models)})"
        )
    return model


def _merge_endpoint(
    device_id: str,
    *,
    host: str,
    port: int | None,
    extensions: dict[str, Any],
    raw_model: DeviceModelRaw,
) -> tuple[ConnectionEndpoint, dict[str, ProtocolOptionValue]]:
    """合并型号连接默认值与实例端点（实例优先）。

    - ``port``：实例优先，否则取 ``connection_defaults['port']``，
      两者皆无是配置错误；
    - 其余 connection_defaults 键并入 device_options，实例 extensions
      同名键覆盖；ADS 额外注入型号 ``read_mode``。
    """
    defaults = dict(raw_model.connection_defaults)
    default_port = defaults.pop("port", None)
    merged_port = port if port is not None else default_port
    if merged_port is None:
        raise ConfigError(
            f"Device '{device_id}': endpoint has no 'port' and the model provides "
            "no 'connection_defaults.port'"
        )
    if not isinstance(merged_port, int | str):
        raise ConfigError(
            f"Device '{device_id}': endpoint port must be an int or numeric string, "
            f"got {type(merged_port).__name__}"
        )

    merged: dict[str, Any] = {**defaults, **extensions}
    if raw_model.protocol == "ads":
        merged["read_mode"] = raw_model.read_mode or "sum"

    options: dict[str, ProtocolOptionValue] = {}
    for key, value in merged.items():
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ConfigError(
                f"Device '{device_id}': connection option '{key}' must be a "
                f"scalar value, got {type(value).__name__}"
            )
        options[key] = value

    try:
        endpoint = ConnectionEndpoint(host=host, port=int(merged_port))
    except ValueError as exc:
        raise ConfigError(f"Device '{device_id}': invalid endpoint: {exc}") from exc
    return endpoint, options


def _build_point(
    table_id: PointTableId,
    protocol: str,
    raw_point: PointConfigRaw,
    units_file: UnitsFile,
    business_points: dict[BusinessPointId, BusinessPoint],
) -> Point:
    """把单条完整 Raw 点映射为 Core Domain Point（并合成 BusinessPoint）。"""
    unit = _resolve_unit(raw_point, units_file)
    data_type = DataType(raw_point.data_type)
    context = f"point table '{table_id}' point '{raw_point.point_id}'"

    if data_type in (DataType.BOOL, DataType.STRING):
        if unit.code is not UnitCode.NONE:
            raise ConfigError(
                f"{context} with {data_type.value} value must use " "dimensionless unit"
            )
        if raw_point.scale != 1.0 or raw_point.offset != 0.0:
            raise ConfigError(
                f"{context} with {data_type.value} value must use " "identity scale/offset"
            )

    business_point_id = _synthesize_business_point(
        table_id,
        raw_point,
        data_type,
        unit,
        business_points,
    )

    ext: dict[str, str | int | float | bool] = {}
    for key, value in raw_point.address.model_dump().items():
        if value is None:
            continue
        if not isinstance(value, str | int | float | bool):
            raise ConfigError(
                f"{context}: address field '{key}' must be a scalar value, "
                f"got {type(value).__name__}"
            )
        ext[key] = value
    ext["data_type"] = raw_point.data_type

    try:
        return Point(
            point_id=raw_point.point_id,
            business_point_id=business_point_id,
            source_unit=unit,
            access=_derive_access(protocol, raw_point),
            scale=raw_point.scale,
            offset=raw_point.offset,
            ext=ext,
        )
    except ValueError as exc:
        raise ConfigError(f"{context} is invalid: {exc}") from exc


def _resolve_unit(raw_point: PointConfigRaw, units_file: UnitsFile) -> Unit:
    """把 point.unit ID 解析为 Core canonical Unit。"""
    unit_id = raw_point.unit
    if unit_id not in units_file.units:
        raise ConfigError(
            f"Point '{raw_point.point_id}': unknown unit '{unit_id}' " "(not defined in units.yaml)"
        )
    try:
        code = UnitCode(unit_id)
    except ValueError as exc:
        raise ConfigError(
            f"Point '{raw_point.point_id}': unit '{unit_id}' is not a "
            "built-in canonical unit code"
        ) from exc
    return UNIT_CATALOG[code]


def _synthesize_business_point(
    table_id: PointTableId,
    raw_point: PointConfigRaw,
    data_type: DataType,
    unit: Unit,
    business_points: dict[BusinessPointId, BusinessPoint],
) -> BusinessPointId:
    """合成 BusinessPoint：默认 id == point_id，跨表冲突加表前缀。"""
    candidate = BusinessPointId(raw_point.point_id)
    existing = business_points.get(candidate)
    if existing is not None:
        if existing.data_type is data_type and existing.standard_unit == unit:
            return candidate
        candidate = BusinessPointId(f"{table_id}:{raw_point.point_id}")
        existing = business_points.get(candidate)
        if existing is not None and (
            existing.data_type is not data_type or existing.standard_unit != unit
        ):
            raise ConfigError(
                f"business point id collision for '{candidate}' with " "conflicting data_type/unit"
            )
        if existing is not None:
            return candidate
    business_points[candidate] = BusinessPoint(
        business_point_id=candidate,
        data_type=data_type,
        standard_unit=unit,
        description=raw_point.description,
    )
    return candidate


def _derive_access(protocol: str, raw_point: PointConfigRaw) -> PointAccess:
    """按协议地址推导点读写能力（配置适配策略）。"""
    if protocol == "ads":
        return PointAccess.READ_WRITE
    if protocol == "iec104":
        type_id = (raw_point.address.type or "").strip().upper()
        if type_id.startswith("C_"):
            return PointAccess.READ_WRITE
        return PointAccess.READ
    # modbus：只读寄存器区不可写（YAML 用 register_type 键，type 为别名）
    address_fields = raw_point.address.model_dump()
    raw_register = address_fields.get("register_type", address_fields.get("type"))
    register_type = str(raw_register or "").strip().lower()
    if register_type in _MODBUS_READ_ONLY:
        return PointAccess.READ
    return PointAccess.READ_WRITE


__all__ = ["build_core_snapshot"]
