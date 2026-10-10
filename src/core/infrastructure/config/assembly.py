"""类型化主题配置 → 共享领域配置索引组装。

把 DeviceModelsConfig / DevicesConfig / PointTablesConfig / UnitsConfig
合并为 CoreConfigAssembly——
一组冻结、经过领域一致性校验的配置索引，同时产出进程级附属配置：

- ``point_meta``：点位的 variable_name / point_groups（采集选点分组与
  展示元数据，不属于协议 Point.ext，也不属于共享 Domain 点定义）；
- ``disabled_devices``：``enabled: false`` 的设备不进快照，记入本集合；
- ``ads_subscribe_devices``：ADS 设备 endpoint extensions 中的
  ``subscribe_enabled: true`` 被提取为本集合——新 Core 的 ADS Driver 严格
  拒绝未知 option，订阅开关不再进入 protocol_options_by_device。

映射规则（与旧系统行为对齐）：

- 端点合并：``port`` 取实例 endpoint.port，缺省取型号
  ``connection_defaults.port``，两者皆无是配置错误；其余
  connection_defaults 键与实例 endpoint.extensions 合并（实例优先）成为
  协议 protocol_options_by_device；ADS 额外注入型号 ``read_mode``（缺省 ``sum``）；
- 单位：``point.unit`` 必须是 Core 内置 canonical unit code，不读取 units.yaml；
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
from core.domain.config import (
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    PointConfig,
    PointTablesConfig,
)
from core.domain.unit import UNIT_CATALOG, Unit, UnitCode
from core.infrastructure.protocol.ads.config import parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config

# 协议名 → 连接参数解析器：装配阶段即完成协议参数合法性校验（第二层
# 配置领域校验），与 Driver 构造使用同一解析入口，避免两套校验漂移。
_PROTOCOL_OPTION_PARSERS = {
    "ads": parse_ads_config,
    "modbus": parse_modbus_config,
    "iec104": parse_iec104_config,
}


@dataclass(frozen=True, slots=True)
class CoreConfigAssembly:
    """冻结并校验过的领域配置索引与进程级附属配置（一次性组装结果）。"""

    device_types: Mapping[DeviceTypeId, DeviceType] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)
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
            "protocol_options_by_device",
            MappingProxyType(
                {
                    key: freeze_protocol_options(value)
                    for key, value in self.protocol_options_by_device.items()
                }
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

    def point_table_for_device(self, device_id: DeviceId) -> PointTable:
        """解析 Device -> DeviceModel -> PointTable。"""
        device = self.devices[device_id]
        model = self.device_models[device.device_model_id]
        return self.point_tables[model.point_table_id]

    def protocol_options_for(self, device_id: DeviceId) -> ProtocolOptions:
        """返回指定 Device 的协议专有连接配置；缺省为空映射。"""
        return self.protocol_options_by_device.get(device_id, MappingProxyType({}))


_MODBUS_READ_ONLY = frozenset({"discrete_input", "discrete", "input", "input_register"})


def assemble_core_config(
    *,
    device_models_config: DeviceModelsConfig,
    devices_config: DevicesConfig,
    point_config: PointTablesConfig,
    defined_business_points: Mapping[BusinessPointId, BusinessPoint],
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
            name=type_definition or type_id,
        )
        for type_id, type_definition in device_models_config.device_types.items()
    }

    business_points: dict[BusinessPointId, BusinessPoint] = dict(defined_business_points)
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
            parent_id=(PointTableId(table_definition.parent_id) if table_definition.parent_id else None),
        )
        point_meta[table_id] = meta

    device_models: dict[DeviceModelId, DeviceModel] = {}
    for model_id, model_definition in device_models_config.device_models.items():
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
    protocol_options_by_device: dict[DeviceId, dict[str, ProtocolOptionValue]] = {}
    group_names: set[str] = set()
    disabled: set[DeviceId] = set()
    ads_subscribe: set[DeviceId] = set()
    for instance in devices_config.devices.values():
        device_id = DeviceId(instance.device_id)
        if not instance.enabled:
            disabled.add(device_id)
        model = _lookup_model(instance.device_id, instance.model, device_models_config)
        model_id = DeviceModelId(instance.model)
        endpoint, options = _merge_endpoint(
            instance.device_id,
            host=instance.endpoint.host,
            port=instance.endpoint.port,
            extensions=instance.endpoint.extensions,
            model=model,
        )
        if instance.device_group is None:
            raise ConfigError(f"Device '{instance.device_id}' requires device_group")
        group_names.add(instance.device_group)
        group_ids = (DeviceGroupId(instance.device_group),)
        devices[device_id] = Device(
            device_id=device_id,
            device_model_id=model_id,
            endpoint=endpoint,
            device_group_ids=group_ids,
        )
        # ADS 订阅开关是 Collector 采集模式选择，不是 Driver 连接参数：
        # 从 protocol_options_by_device 剥离（Core ADS Driver 严格拒绝未知 option），
        # 记入 ads_subscribe_devices。
        if model.protocol == "ads" and options.pop("subscribe_enabled", False):
            ads_subscribe.add(device_id)
        _validate_protocol_options(instance.device_id, model.protocol, endpoint, options)
        protocol_options_by_device[device_id] = options

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
            protocol_options_by_device=protocol_options_by_device,
        )
        assembly = CoreConfigAssembly(
            device_types=device_types,
            device_models=device_models,
            device_groups=device_groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            protocol_options_by_device=protocol_options_by_device,
            point_meta=point_meta,
            disabled_devices=frozenset(disabled),
            ads_subscribe_devices=frozenset(ads_subscribe),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid core configuration: {exc}") from exc

    return assembly


def _validate_protocol_options(
    device_id: str,
    protocol: str,
    endpoint: ConnectionEndpoint,
    options: Mapping[str, ProtocolOptionValue],
) -> None:
    """装配阶段校验协议连接参数（与 Driver 构造共用同一解析器）。

    使非法 option（未知键、非法取值）在配置加载/热更新 prepare 阶段即
    被拒绝，而不是推迟到 Driver 实例化才暴露。
    """
    parser = _PROTOCOL_OPTION_PARSERS.get(protocol)
    if parser is None:
        raise ConfigError(f"Device '{device_id}': unsupported protocol '{protocol}'")
    try:
        parser(endpoint, options)
    except ConfigError as exc:
        raise ConfigError(f"Device '{device_id}': {exc}") from exc


def _lookup_model(
    device_id: str,
    model_id: str,
    device_models_config: DeviceModelsConfig,
) -> DeviceModelConfig:
    model = device_models_config.device_models.get(model_id)
    if model is None:
        raise ConfigError(
            f"Device '{device_id}' references unknown model '{model_id}' "
            f"(available: {sorted(device_models_config.device_models)})"
        )
    return model


def _merge_endpoint(
    device_id: str,
    *,
    host: str,
    port: int | None,
    extensions: Mapping[str, Any],
    model: DeviceModelConfig,
) -> tuple[ConnectionEndpoint, dict[str, ProtocolOptionValue]]:
    """合并型号连接默认值与实例端点（实例优先）。

    - ``port``：实例优先，否则取 ``connection_defaults['port']``，
      两者皆无是配置错误；
    - 其余 connection_defaults 键并入 protocol_options_by_device，实例 extensions
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
    definition: PointConfig,
    business_points: dict[BusinessPointId, BusinessPoint],
) -> Point:
    """把单条类型化点定义映射为 Core Domain Point（并合成 BusinessPoint）。"""
    unit = _resolve_unit(definition)
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


def _resolve_unit(definition: PointConfig) -> Unit:
    """直接从内置标准单位目录解析，不使用 units.yaml。"""
    try:
        code = UnitCode(definition.unit)
    except ValueError as exc:
        raise ConfigError(
            f"Point '{definition.point_id}': unknown built-in unit '{definition.unit}'"
        ) from exc
    return UNIT_CATALOG[code]


def _synthesize_business_point(
    table_id: PointTableId,
    definition: PointConfig,
    data_type: DataType,
    unit: Unit,
    business_points: dict[BusinessPointId, BusinessPoint],
) -> BusinessPointId:
    """合成 BusinessPoint：默认 id == point_id，跨表冲突加表前缀。"""
    if definition.business_point_id is None:
        raise ConfigError(
            f"point table '{table_id}' point '{definition.point_id}' "
            "must specify business_point_id"
        )
    candidate = BusinessPointId(definition.business_point_id)
    existing = business_points.get(candidate)
    if existing is None:
        raise ConfigError(
            f"point table '{table_id}' point '{definition.point_id}' "
            f"references unknown business point '{candidate}'"
        )
    if existing.data_type is not data_type or existing.standard_unit.quantity != unit.quantity:
        raise ConfigError(
            f"point table '{table_id}' point '{definition.point_id}' "
            f"incompatible with business point '{candidate}'"
        )
    return candidate


def _derive_access(protocol: str, definition: PointConfig) -> PointAccess:
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
