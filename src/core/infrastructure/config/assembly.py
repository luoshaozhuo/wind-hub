"""类型化主题配置 → 共享领域配置索引组装。

把 DeviceConfig / PointConfig / UnitConfig 合并为 CoreConfigAssembly——
一组冻结、经过领域一致性校验的配置索引，同时产出进程级附属配置：

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

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from core.application import ConfigError
from core.application.config_types import (
    DeviceConfig,
    DeviceModelDefinition,
    PointDefinition,
    PointTablesConfig,
    UnitConfig,
)
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionEndpoint,
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
    PointMeta,
    PointTable,
    PointTableId,
    Protocol,
    ProtocolOptions,
    ProtocolOptionValue,
    freeze_protocol_options,
    validate_core_config,
)
from core.domain.unit import UNIT_CATALOG, Unit, UnitCode


@dataclass(frozen=True, slots=True)
class CoreConfigAssembly:
    """冻结并校验过的领域配置索引与进程级附属配置（一次性组装结果）。"""

    device_types: Mapping[DeviceTypeId, DeviceType] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    device_options: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]] = field(default_factory=dict)
    disabled_devices: frozenset[DeviceId] = frozenset()
    ads_subscribe_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
        for name in (
            "device_types",
            "device_models",
            "device_groups",
            "devices",
            "business_points",
            "point_tables",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(
            self,
            "device_options",
            MappingProxyType(
                {key: freeze_protocol_options(value) for key, value in self.device_options.items()}
            ),
        )
        object.__setattr__(
            self,
            "point_meta",
            MappingProxyType(
                {
                    table_id: MappingProxyType(dict(meta))
                    for table_id, meta in self.point_meta.items()
                }
            ),
        )
        object.__setattr__(self, "disabled_devices", frozenset(self.disabled_devices))
        object.__setattr__(self, "ads_subscribe_devices", frozenset(self.ads_subscribe_devices))


_MODBUS_READ_ONLY = frozenset({"discrete_input", "discrete", "input", "input_register"})


def assemble_core_config(
    *,
    device_config: DeviceConfig,
    point_config: PointTablesConfig,
    unit_config: UnitConfig,
) -> CoreConfigAssembly:
    """合并类型化主题配置为冻结的领域配置索引 + 进程级附属配置。

    Returns:
        CoreConfigAssembly；point_meta 为 ``{点表: {point_id: PointMeta}}``。

    Raises:
        ConfigError: 任何引用缺失、协议不一致或领域不变量违反。
    """
    device_types = {
        DeviceTypeId(type_id): DeviceType(
            device_type_id=DeviceTypeId(type_id),
            name=type_definition.name or type_id,
        )
        for type_id, type_definition in device_config.models.device_types.items()
    }

    business_points: dict[BusinessPointId, BusinessPoint] = {}
    point_tables: dict[PointTableId, PointTable] = {}
    point_meta: dict[PointTableId, dict[str, PointMeta]] = {}
    for table_name, table_definition in point_config.tables.items():
        table_id = PointTableId(table_name)
        points: dict[str, Point] = {}
        meta: dict[str, PointMeta] = {}
        for point_definition in table_definition.points.values():
            point = _build_point(
                table_id,
                table_definition.protocol,
                point_definition,
                unit_config,
                business_points,
            )
            points[point.point_id] = point
            meta[point.point_id] = PointMeta(
                variable_name=point_definition.variable_name,
                point_groups=point_definition.point_groups,
            )
        point_tables[table_id] = PointTable(
            point_table_id=table_id,
            protocol=Protocol(table_definition.protocol),
            points=points,
        )
        point_meta[table_id] = meta

    device_models: dict[DeviceModelId, DeviceModel] = {}
    for model_id, model_definition in device_config.models.device_models.items():
        table = point_tables.get(PointTableId(model_definition.point_table))
        if table is None:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table "
                f"'{model_definition.point_table}'"
            )
        if table.protocol.name != model_definition.protocol:
            raise ConfigError(
                f"Device model '{model_id}' protocol '{model_definition.protocol}' "
                f"does not match point table '{model_definition.point_table}' "
                f"protocol '{table.protocol.name}'"
            )
        device_models[DeviceModelId(model_id)] = DeviceModel(
            device_model_id=DeviceModelId(model_id),
            device_type_id=DeviceTypeId(model_definition.device_type),
            point_table_id=PointTableId(model_definition.point_table),
            name=model_definition.model,
            manufacturer=model_definition.manufacturer,
        )

    devices: dict[DeviceId, Device] = {}
    device_options: dict[DeviceId, dict[str, ProtocolOptionValue]] = {}
    group_names: set[str] = set()
    disabled: set[DeviceId] = set()
    ads_subscribe: set[DeviceId] = set()
    for instance in device_config.instances.devices:
        device_id = DeviceId(instance.device_id)
        if not instance.enabled:
            disabled.add(device_id)
            continue
        model = _lookup_model(instance.device_id, instance.model, device_config)
        model_id = DeviceModelId(instance.model)
        endpoint, options = _merge_endpoint(
            instance.device_id,
            host=instance.endpoint.host,
            port=instance.endpoint.port,
            extensions=instance.endpoint.extensions,
            model=model,
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
        if model.protocol == "ads" and options.pop("subscribe_enabled", False):
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
        validate_core_config(
            device_types=device_types,
            device_models=device_models,
            device_groups=device_groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            device_options=device_options,
        )
        assembly = CoreConfigAssembly(
            device_types=device_types,
            device_models=device_models,
            device_groups=device_groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            device_options=device_options,
            point_meta=point_meta,
            disabled_devices=frozenset(disabled),
            ads_subscribe_devices=frozenset(ads_subscribe),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid core configuration: {exc}") from exc

    return assembly


def _lookup_model(
    device_id: str,
    model_id: str,
    device_config: DeviceConfig,
) -> DeviceModelDefinition:
    model = device_config.models.device_models.get(model_id)
    if model is None:
        raise ConfigError(
            f"Device '{device_id}' references unknown model '{model_id}' "
            f"(available: {sorted(device_config.models.device_models)})"
        )
    return model


def _merge_endpoint(
    device_id: str,
    *,
    host: str,
    port: int | None,
    extensions: Mapping[str, Any],
    model: DeviceModelDefinition,
) -> tuple[ConnectionEndpoint, dict[str, ProtocolOptionValue]]:
    """合并型号连接默认值与实例端点（实例优先）。

    - ``port``：实例优先，否则取 ``connection_defaults['port']``，
      两者皆无是配置错误；
    - 其余 connection_defaults 键并入 device_options，实例 extensions
      同名键覆盖；ADS 额外注入型号 ``read_mode``。
    """
    defaults = dict(model.connection_defaults)
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
    if model.protocol == "ads":
        merged["read_mode"] = model.read_mode or "sum"

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
    definition: PointDefinition,
    unit_config: UnitConfig,
    business_points: dict[BusinessPointId, BusinessPoint],
) -> Point:
    """把单条类型化点定义映射为 Core Domain Point（并合成 BusinessPoint）。"""
    unit = _resolve_unit(definition, unit_config)
    data_type = DataType(definition.data_type)
    context = f"point table '{table_id}' point '{definition.point_id}'"

    if data_type in (DataType.BOOL, DataType.STRING):
        if unit.code is not UnitCode.NONE:
            raise ConfigError(
                f"{context} with {data_type.value} value must use " "dimensionless unit"
            )
        if definition.scale != 1.0 or definition.offset != 0.0:
            raise ConfigError(
                f"{context} with {data_type.value} value must use " "identity scale/offset"
            )

    business_point_id = _synthesize_business_point(
        table_id,
        definition,
        data_type,
        unit,
        business_points,
    )

    ext: dict[str, str | int | float | bool] = {}
    for key, value in definition.address.items():
        if not isinstance(value, str | int | float | bool):
            raise ConfigError(
                f"{context}: address field '{key}' must be a scalar value, "
                f"got {type(value).__name__}"
            )
        ext[key] = value
    ext["data_type"] = definition.data_type

    try:
        return Point(
            point_id=definition.point_id,
            business_point_id=business_point_id,
            source_unit=unit,
            access=_derive_access(protocol, definition),
            scale=definition.scale,
            offset=definition.offset,
            ext=ext,
        )
    except ValueError as exc:
        raise ConfigError(f"{context} is invalid: {exc}") from exc


def _resolve_unit(definition: PointDefinition, unit_config: UnitConfig) -> Unit:
    """把 point.unit ID 解析为 Core canonical Unit。"""
    unit_id = definition.unit
    if unit_id not in unit_config.units:
        raise ConfigError(
            f"Point '{definition.point_id}': unknown unit '{unit_id}' "
            "(not defined in units.yaml)"
        )
    try:
        code = UnitCode(unit_id)
    except ValueError as exc:
        raise ConfigError(
            f"Point '{definition.point_id}': unit '{unit_id}' is not a "
            "built-in canonical unit code"
        ) from exc
    return UNIT_CATALOG[code]


def _synthesize_business_point(
    table_id: PointTableId,
    definition: PointDefinition,
    data_type: DataType,
    unit: Unit,
    business_points: dict[BusinessPointId, BusinessPoint],
) -> BusinessPointId:
    """合成 BusinessPoint：默认 id == point_id，跨表冲突加表前缀。"""
    candidate = BusinessPointId(definition.point_id)
    existing = business_points.get(candidate)
    if existing is not None:
        if existing.data_type is data_type and existing.standard_unit == unit:
            return candidate
        candidate = BusinessPointId(f"{table_id}:{definition.point_id}")
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
        description=definition.description,
    )
    return candidate


def _derive_access(protocol: str, definition: PointDefinition) -> PointAccess:
    """按协议地址推导点读写能力（配置适配策略）。"""
    if protocol == "ads":
        return PointAccess.READ_WRITE
    if protocol == "iec104":
        type_id = str(definition.address.get("type") or "").strip().upper()
        if type_id.startswith("C_"):
            return PointAccess.READ_WRITE
        return PointAccess.READ
    # modbus：只读寄存器区不可写（YAML 用 register_type 键，type 为别名）
    raw_register = definition.address.get("register_type", definition.address.get("type"))
    register_type = str(raw_register or "").strip().lower()
    if register_type in _MODBUS_READ_ONLY:
        return PointAccess.READ
    return PointAccess.READ_WRITE


__all__ = ["CoreConfigAssembly", "PointMeta", "assemble_core_config"]
