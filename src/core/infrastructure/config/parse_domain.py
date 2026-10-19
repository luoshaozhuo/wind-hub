"""YAML -> Core Domain 的直接解析。无 Config VO 中间模型。

统一 Schema 约定：

- 具有稳定唯一 ID 的实体集合一律使用 Mapping，Mapping Key 即实体 ID，
  实体内部不重复声明自身 ID；
- 跨实体引用字段统一命名：``model_id`` / ``device_type_id`` /
  ``point_table_id`` / ``device_group_id(s)`` / ``sink_ids`` /
  ``business_point_id`` / ``parent_id``；
- ``endpoint`` 只表达 host/port；协议参数一律放在设备或型号的
  ``protocol_options``；型号连接默认值拆分为 ``endpoint_defaults``
  （仅 port）与 ``protocol_options``；
- 快照只保存显式配置：``Device.endpoint`` 与
  ``Device.protocol_options`` 均为设备实例显式值，型号默认值留在
  ``DeviceModel`` 上，运行时有效参数由 Collector/Commander 装配层合并。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.settings import (
    ADSLocalConfig,
    LogSettings,
    RuntimeSettings,
    SystemSettings,
    ThreadPoolSettings,
)
from core.domain import (
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
    PointTable,
    PointTableId,
    Protocol,
    Sink,
    Site,
    Task,
    validate_core_config,
)
from core.domain.task import TaskOutput
from core.domain.unit import DIMENSIONLESS, UnitCatalog
from core.infrastructure.protocol.ads.config import is_valid_ams_net_id, parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config

from .business_points import parse_business_points
from .sink_schema import parse_sink_definitions, validate_task_sink_points
from .units import parse_unit_catalog
from .yaml import read_yaml_mapping
from .yaml import yaml_plain as _yaml_plain

_OPTION_PARSERS = {
    "ads": parse_ads_config,
    "modbus": parse_modbus_config,
    "iec104": parse_iec104_config,
}
_POINT_FIELDS = {
    "point_groups",
    "address",
    "data_type",
    "scale",
    "offset",
    "unit",
    "description",
    "business_point_id",
}
_DEVICE_FIELDS = {
    "model_id",
    "device_group_ids",
    "endpoint",
    "protocol_options",
    "enabled",
    "name",
}
_MODEL_FIELDS = {
    "device_type_id",
    "manufacturer",
    "model",
    "protocol",
    "point_table_id",
    "properties",
    "endpoint_defaults",
    "protocol_options",
}


def _map(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{context} must be a mapping with string keys")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{context} must be a list")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ConfigError(f"{context} unknown keys: {sorted(extra)}")


def _name(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value.strip()


def _id_key(value: Any, context: str) -> str:
    """实体 Mapping Key（即实体 ID）的严格校验。"""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigError(f"{context}: entity IDs must be non-empty trimmed strings")
    return value


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ConfigError(f"{context} must be a finite number")
    return float(value)


def _flag(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be boolean")
    return value


def _port(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(f"{context} must be an integer port")
    if isinstance(value, str):
        if not value.isdecimal():
            raise ConfigError(f"{context} must be numeric")
        value = int(value)
    if not 1 <= value <= 65535:
        raise ConfigError(f"{context} must be in 1..65535")
    return int(value)


def _options(value: Any, context: str) -> dict[str, Any]:
    """协议参数集合校验：字符串键、标量值、浮点有限。"""
    raw = _map(value, context)
    options: dict[str, Any] = {}
    for key, item in raw.items():
        if not key.strip():
            raise ConfigError(f"{context} option keys must be non-empty")
        if item is not None and not isinstance(item, str | int | float | bool):
            raise ConfigError(f"{context}.{key} must be a scalar")
        if isinstance(item, float) and not isfinite(item):
            raise ConfigError(f"{context}.{key} must be finite")
        options[key] = item
    return options


def _system(raw: Mapping[str, Any]) -> tuple[str, str, SystemSettings]:
    site = _map(raw.get("site"), "system.site")
    _keys(site, {"site_id", "name"}, "system.site")
    site_id = _name(site.get("site_id"), "system.site_id")
    name = _name(site.get("name", site_id), "system.site.name")
    runtime_raw = _map(raw.get("runtime", {}), "system.runtime")
    _keys(runtime_raw, set(RuntimeSettings.__dataclass_fields__), "system.runtime")
    settings = dict(runtime_raw)
    for key in (
        "shutdown_timeout",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "watchdog_interval",
        "health_timeout",
    ):
        if key in settings and settings[key] is not None:
            settings[key] = _number(settings[key], f"runtime.{key}")
    if "queue_maxsize" in settings and settings["queue_maxsize"] is not None:
        value = settings["queue_maxsize"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("runtime.queue_maxsize must be an integer")
    thread_pool_raw = settings.pop("thread_pool", None)
    if thread_pool_raw is not None:
        pool = _map(thread_pool_raw, "system.runtime.thread_pool")
        _keys(pool, set(ThreadPoolSettings.__dataclass_fields__), "system.runtime.thread_pool")
        max_workers = pool.get("max_workers", 32)
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise ConfigError("runtime.thread_pool.max_workers must be an integer")
        settings["thread_pool"] = ThreadPoolSettings(max_workers=max_workers)
    runtime = RuntimeSettings(**settings)
    ads_raw = raw.get("ads")
    ads = None
    if ads_raw is not None:
        values = _map(ads_raw, "system.ads")
        _keys(values, set(ADSLocalConfig.__dataclass_fields__), "system.ads")
        net_id = _name(values.get("local_ams_net_id"), "ads.local_ams_net_id")
        if not is_valid_ams_net_id(net_id):
            raise ConfigError(f"invalid AMS Net ID '{net_id}'")
        ads = ADSLocalConfig(
            local_ams_net_id=net_id,
            local_ip=_name(values.get("local_ip"), "ads.local_ip"),
        )
    # system.yaml 顶层允许透传非托管段（如 interfaces）：域模型不解析，
    # 保存时由 YamlConfigAdapter 原样合并写回。已知段在上方各自严格校验。
    logs_raw = _map(raw.get("logs", {}), "system.logs")
    _keys(logs_raw, set(LogSettings.__dataclass_fields__), "system.logs")
    logs = LogSettings(**logs_raw)
    return site_id, name, SystemSettings(runtime=runtime, ads=ads, logs=logs)


_ADS_LOGICAL_TYPES: dict[str, str] = {
    "BOOL": "bool", "SINT": "int8", "USINT": "uint8",
    "INT": "int16", "UINT": "uint16", "DINT": "int32",
    "UDINT": "uint32", "LINT": "int64", "ULINT": "uint64",
    "REAL": "float32", "LREAL": "float64", "STRING": "str",
}


def _point_data_type(value: str, protocol: str) -> tuple[DataType, str]:
    """解析协议测点类型，同时保留协议侧原始表示供 Driver 使用。"""
    if protocol == "ads":
        native_type = value.upper()
        logical = _ADS_LOGICAL_TYPES.get(native_type)
        if logical is None:
            raise ConfigError(f"unsupported ADS data_type '{value}'")
        return DataType(logical), native_type
    # Modbus 数据编码类型与标准标量命名一致；IEC104 的协议类型
    # 使用 address.type_id，data_type 表达 ASDU 值的 Python 标量类型。
    return DataType(value), value


def _point(
    point_id: str,
    entry: Mapping[str, Any],
    protocol: str,
    business_points: Mapping[BusinessPointId, Any],
    units: UnitCatalog,
) -> Point:
    context = f"point '{point_id}'"
    _keys(entry, _POINT_FIELDS, context)
    raw_business_id = entry.get("business_point_id", point_id)
    identity = BusinessPointId(
        _name(raw_business_id, f"{context}.business_point_id")
    )
    business_point = business_points.get(identity)
    if business_point is None:
        raise ConfigError(f"{context} unknown business point '{identity}'")
    try:
        data_type, native_type = _point_data_type(
            _name(entry.get("data_type"), f"{context}.data_type"), protocol
        )
        unit = units.unit(_name(entry.get("unit", "none"), f"{context}.unit"))
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"{context}: invalid data type or unit: {exc}") from exc
    if (
        data_type != business_point.data_type
        or unit.quantity != business_point.standard_unit.quantity
    ):
        raise ConfigError(f"{context}: incompatible business point '{identity}'")
    scale = _number(entry.get("scale", 1.0), f"{context}.scale")
    offset = _number(entry.get("offset", 0.0), f"{context}.offset")
    if data_type in (DataType.BOOL, DataType.STRING) and (
        unit.quantity != DIMENSIONLESS
        or not unit.is_base_transform
        or scale != 1.0
        or offset != 0.0
    ):
        raise ConfigError(f"{context}: bool/string must be dimensionless without scaling")

    address = _map(entry.get("address"), f"{context}.address")
    extensions: dict[str, Any] = {}
    for key, value in address.items():
        if value is None or not isinstance(value, str | int | float | bool):
            raise ConfigError(f"{context}.address.{key} must be a scalar")
        if isinstance(value, float) and not isfinite(value):
            raise ConfigError(f"{context}.address.{key} must be finite")
        extensions[key] = value
    extensions["data_type"] = native_type
    groups = tuple(
        _name(name, f"{context}.point_groups")
        for name in _list(entry.get("point_groups", []), f"{context}.point_groups")
    )
    if protocol == "ads":
        access = PointAccess.READ_WRITE
    elif protocol == "iec104":
        access = (
            PointAccess.READ_WRITE
            if str(address.get("type", "")).upper().startswith("C_")
            else PointAccess.READ
        )
    else:
        register = str(address.get("register_type", address.get("type", ""))).lower()
        access = (
            PointAccess.READ
            if register in {"discrete_input", "discrete", "input", "input_register"}
            else PointAccess.READ_WRITE
        )
    return Point(
        point_id=point_id,
        business_point_id=identity,
        source_unit=unit,
        access=access,
        scale=scale,
        offset=offset,
        ext=extensions,
        description=entry.get("description"),
        point_groups=groups,
    )


def _point_tables(
    raw: Mapping[str, Any],
    business_points: Mapping[BusinessPointId, Any],
    units: UnitCatalog,
) -> dict[PointTableId, PointTable]:
    _keys(raw, {"point_tables"}, "points.yaml")
    definitions = _map(raw.get("point_tables"), "points.point_tables")
    cache: dict[str, PointTable] = {}
    expanded: dict[str, dict[str, dict[str, Any]]] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> PointTable:
        if name in cache:
            return cache[name]
        if name in visiting:
            raise ConfigError(f"point table inheritance cycle: {name}")
        if name not in definitions:
            raise ConfigError(f"point table '{name}' not found")
        visiting.add(name)
        definition = _map(definitions[name], f"point table '{name}'")
        _keys(
            definition,
            {"protocol", "parent_id", "remove_points", "points"},
            f"point table '{name}'",
        )
        parent_id = definition.get("parent_id")
        parent = (
            resolve(_name(parent_id, f"{name}.parent_id")) if parent_id is not None else None
        )
        protocol = definition.get("protocol", parent.protocol.name if parent else None)
        if protocol not in _OPTION_PARSERS:
            raise ConfigError(f"point table '{name}' unsupported protocol '{protocol}'")
        if parent is not None and parent.protocol.name != protocol:
            raise ConfigError(f"point table '{name}' protocol differs from parent")
        points = (
            {key: dict(value) for key, value in expanded[str(parent_id)].items()}
            if parent is not None
            else {}
        )

        remove = _list(definition.get("remove_points", []), f"{name}.remove_points")
        if len(remove) != len(set(remove)):
            raise ConfigError(f"point table '{name}' duplicate remove_points")
        for key in remove:
            removed = _name(key, f"{name}.remove_points")
            if removed not in points:
                raise ConfigError(f"point table '{name}' cannot remove unknown point '{removed}'")
            del points[removed]
        patches = _map(definition.get("points", {}), f"{name}.points")
        for raw_key, item in patches.items():
            key = _id_key(raw_key, f"point table '{name}'.points")
            patch = _map(item, f"{name}.points.{key}")
            _keys(patch, _POINT_FIELDS, f"{name}.points.{key}")
            updated = dict(points.get(key, {}))
            updated.update(patch)
            points[key] = updated
        domain_points = {
            key: _point(key, value, protocol, business_points, units)
            for key, value in points.items()
        }
        table = PointTable(
            point_table_id=PointTableId(name),
            protocol=Protocol(protocol),
            points=domain_points,
            parent_id=PointTableId(parent_id) if parent_id else None,
        )
        cache[name] = table
        expanded[name] = points
        visiting.remove(name)
        return table

    for raw_key in definitions:
        resolve(_id_key(raw_key, "point table ID"))
    return {PointTableId(key): table for key, table in cache.items()}


def _device_types(raw: Mapping[str, Any]) -> dict[DeviceTypeId, DeviceType]:
    types: dict[DeviceTypeId, DeviceType] = {}
    for raw_key, item in _map(raw.get("device_types", {}), "device_types").items():
        key = _id_key(raw_key, "device type ID")
        definition = _map(item, f"device type '{key}'")
        _keys(definition, {"name", "description"}, f"device type '{key}'")
        identity = DeviceTypeId(key)
        types[identity] = DeviceType(
            device_type_id=identity,
            name=_name(definition.get("name", key), f"device type '{key}'.name"),
            description=definition.get("description"),
        )
    return types


def _device_models(
    raw: Mapping[str, Any],
    types: Mapping[DeviceTypeId, DeviceType],
    tables: Mapping[PointTableId, PointTable],
) -> dict[DeviceModelId, DeviceModel]:
    models: dict[DeviceModelId, DeviceModel] = {}
    for raw_key, item in _map(raw.get("device_models", {}), "device_models").items():
        key = _id_key(raw_key, "device model ID")
        definition = _map(item, f"device model '{key}'")
        _keys(definition, _MODEL_FIELDS, f"device model '{key}'")
        identity = DeviceModelId(key)
        type_id = DeviceTypeId(_name(definition.get("device_type_id"), "model.device_type_id"))
        table_id = PointTableId(
            _name(definition.get("point_table_id"), "model.point_table_id")
        )
        protocol = _name(definition.get("protocol"), "model.protocol")
        if type_id not in types:
            raise ConfigError(f"device model '{key}' references unknown device type '{type_id}'")
        if table_id not in tables:
            raise ConfigError(
                f"device model '{key}' references unknown point table '{table_id}'"
            )
        if tables[table_id].protocol.name != protocol:
            raise ConfigError(f"device model '{key}' protocol does not match point table")
        if protocol not in _OPTION_PARSERS:
            raise ConfigError(f"unsupported protocol '{protocol}'")

        endpoint_defaults = _options(
            definition.get("endpoint_defaults", {}), f"model '{key}'.endpoint_defaults"
        )
        unknown_endpoint = set(endpoint_defaults) - {"port"}
        if unknown_endpoint:
            raise ConfigError(
                f"model '{key}'.endpoint_defaults only supports 'port', "
                f"got {sorted(unknown_endpoint)}"
            )
        if "port" in endpoint_defaults:
            endpoint_defaults["port"] = _port(
                endpoint_defaults["port"], f"model '{key}'.endpoint_defaults.port"
            )
        options = _options(
            definition.get("protocol_options", {}), f"model '{key}'.protocol_options"
        )
        # 用代码默认端口 + 型号默认参数校验型号协议参数本身合法。
        try:
            _OPTION_PARSERS[protocol](ConnectionEndpoint(host="localhost"), options)
        except Exception as exc:
            raise ConfigError(
                f"device model '{key}' invalid protocol options: {exc}"
            ) from exc
        models[identity] = DeviceModel(
            device_model_id=identity,
            device_type_id=type_id,
            point_table_id=table_id,
            name=definition.get("model"),
            manufacturer=definition.get("manufacturer"),
            properties=_map(definition.get("properties", {}), f"model '{key}'.properties"),
            endpoint_defaults=endpoint_defaults,
            protocol_options=options,
        )
    return models


def _devices(
    raw: Mapping[str, Any],
    models: Mapping[DeviceModelId, DeviceModel],
    tables: Mapping[PointTableId, PointTable],
) -> tuple[
    dict[DeviceGroupId, DeviceGroup],
    dict[DeviceId, Device],
]:
    groups: dict[DeviceGroupId, DeviceGroup] = {}
    for raw_key, item in _map(raw.get("device_groups", {}), "devices.device_groups").items():
        key = _id_key(raw_key, "device group ID")
        data = _map(item, f"device group '{key}'")
        _keys(data, {"name", "description"}, f"device group '{key}'")
        group_identity = DeviceGroupId(key)
        groups[group_identity] = DeviceGroup(
            device_group_id=group_identity,
            name=_name(data.get("name", key), f"device group '{key}'.name"),
            description=data.get("description"),
        )

    devices: dict[DeviceId, Device] = {}
    for raw_key, item in _map(raw.get("devices", {}), "devices").items():
        key = _id_key(raw_key, "device ID")
        identity = DeviceId(key)
        context = f"device '{key}'"
        definition = _map(item, context)
        _keys(definition, _DEVICE_FIELDS, context)
        model_id = DeviceModelId(_name(definition.get("model_id"), f"{context}.model_id"))
        model = models.get(model_id)
        if model is None:
            raise ConfigError(f"{context} unknown model '{model_id}'")
        protocol = tables[model.point_table_id].protocol.name

        memberships = tuple(
            DeviceGroupId(_name(group, f"{context}.device_group_ids"))
            for group in _list(
                definition.get("device_group_ids"), f"{context}.device_group_ids"
            )
        )
        for group in memberships:
            if group not in groups:
                raise ConfigError(f"{context} references unknown device group '{group}'")

        raw_endpoint = _map(definition.get("endpoint"), f"{context}.endpoint")
        _keys(raw_endpoint, {"host", "port"}, f"{context}.endpoint")
        port_value = raw_endpoint.get("port")
        endpoint = ConnectionEndpoint(
            host=_name(raw_endpoint.get("host"), f"{context}.endpoint.host"),
            port=(
                _port(port_value, f"{context}.endpoint.port")
                if port_value is not None
                else None
            ),
        )

        options = _options(definition.get("protocol_options", {}), f"{context}.protocol_options")
        subscribe = options.pop("subscribe_enabled", None)
        if subscribe is not None:
            if protocol != "ads":
                raise ConfigError(f"{context}: subscribe_enabled is only valid for ADS devices")
            if not isinstance(subscribe, bool):
                raise ConfigError(f"{context}.protocol_options.subscribe_enabled must be boolean")
        # 按「设备显式 > 型号默认 > 协议代码默认」校验有效参数组合。
        effective_port = endpoint.port or model.endpoint_defaults.get("port")
        effective_endpoint = ConnectionEndpoint(host=endpoint.host, port=effective_port)
        effective_options = {**model.protocol_options, **options}
        try:
            _OPTION_PARSERS[protocol](effective_endpoint, effective_options)
        except Exception as exc:
            raise ConfigError(f"{context} invalid protocol options: {exc}") from exc

        devices[identity] = Device(
            device_id=identity,
            device_model_id=model_id,
            endpoint=endpoint,
            protocol_options=options,
            subscribe_enabled=bool(subscribe),
            name=definition.get("name"),
            device_group_ids=memberships,
            enabled=_flag(definition.get("enabled", True), f"{context}.enabled"),
        )
    return groups, devices


def _tasks(raw: Mapping[str, Any]) -> dict[str, Task]:
    _keys(raw, {"tasks"}, "tasks.yaml")
    result: dict[str, Task] = {}
    for raw_key, item in _map(raw.get("tasks", {}), "tasks").items():
        identity = _id_key(raw_key, "task ID")
        context = f"task '{identity}'"
        data = _map(item, context)
        _keys(
            data,
            {"device_group_id", "point_group", "interval", "outputs", "enabled"},
            context,
        )
        resolved: list[TaskOutput] = []
        for index, item in enumerate(_list(data.get("outputs"), f"{context}.outputs")):
            entry = _map(item, f"{context}.outputs[{index}]")
            _keys(entry, {"sink_id", "key_prefix", "points"}, f"{context}.outputs[{index}]")
            resolved.append(
                TaskOutput(
                    sink_id=_name(entry.get("sink_id"), f"{context}.outputs[{index}].sink_id"),
                    key_prefix=(
                        _name(entry["key_prefix"], f"{context}.outputs[{index}].key_prefix")
                        if "key_prefix" in entry else None
                    ),
                    points=tuple(
                        _map(point, f"{context}.outputs[{index}].points")
                        for point in _list(
                            entry.get("points", []), f"{context}.outputs[{index}].points"
                        )
                    ),
                )
            )
        outputs = tuple(resolved)
        sink_ids = tuple(output.sink_id for output in outputs)
        result[identity] = Task(
            task_id=identity,
            device_group_id=DeviceGroupId(
                _name(data.get("device_group_id"), f"{context}.device_group_id")
            ),
            point_group=_name(data.get("point_group"), f"{context}.point_group"),
            sink_ids=sink_ids,
            outputs=outputs,
            interval=_number(data.get("interval"), f"{context}.interval"),
            enabled=_flag(data.get("enabled", True), f"{context}.enabled"),
        )
    return result


def _sinks(raw: Mapping[str, Any]) -> dict[str, Sink]:
    sinks: dict[str, Sink] = {}
    for sink_id, definition in parse_sink_definitions(raw).items():
        entry = _yaml_plain(definition.model_dump(mode="python"))
        sinks[sink_id] = Sink(
            sink_id=sink_id,
            kind=definition.type,
            enabled=definition.enabled,
            connection=entry["connection"],
        )
    return sinks


def load_domain(config_dir: Path) -> ConfigSnapshot:
    """一次性解析完整 YAML 集合，构建并验证领域快照。"""
    base = Path(config_dir)
    system = read_yaml_mapping(base / "system.yaml")
    units_document = read_yaml_mapping(base / "units.yaml")
    model_document = read_yaml_mapping(base / "device_models.yaml")
    device_document = read_yaml_mapping(base / "devices.yaml")
    point_document = read_yaml_mapping(base / "points.yaml")
    task_document = read_yaml_mapping(base / "tasks.yaml")
    sink_document = read_yaml_mapping(base / "sinks.yaml")
    business_document = read_yaml_mapping(base / "business_points.yaml")
    try:
        site_id, site_name, settings = _system(system)
        units = parse_unit_catalog(units_document)
        business_points = parse_business_points(business_document, units)
        point_tables = _point_tables(point_document, business_points, units)
        _keys(model_document, {"device_types", "device_models"}, "device_models.yaml")
        types = _device_types(model_document)
        models = _device_models(model_document, types, point_tables)
        _keys(device_document, {"devices", "device_groups"}, "devices.yaml")
        groups, devices = _devices(
            device_document,
            models,
            point_tables,
        )
        tasks = _tasks(task_document)
        sinks = _sinks(sink_document)
        for task in tasks.values():
            for output in task.outputs:
                sink = sinks.get(output.sink_id)
                if sink is None:
                    raise ConfigError(
                        f"Task '{task.task_id}' references unknown sink '{output.sink_id}'"
                    )
                if output.key_prefix is not None and sink.kind != "redis":
                    raise ConfigError(
                        f"Task '{task.task_id}' key_prefix requires a Redis Sink"
                    )
                if not output.points and sink.kind == "modbus":
                    # Modbus 从站必须为每个点显式分配寄存器地址；空 points
                    # 无法安全展开为通配（redis/tdengine 可按任务组全量投递）。
                    raise ConfigError(
                        f"Task '{task.task_id}' modbus output '{output.sink_id}' "
                        "must declare explicit points with register addresses"
                    )
                if output.points:
                    # TaskOutput.points 在领域模型内被递归冻结（MappingProxy/tuple），
                    # 校验与 Sink 类型规则一律使用还原后的普通 dict/list 视图。
                    plain_points = [_yaml_plain(point) for point in output.points]
                    for mapping in plain_points:
                        source = _map(mapping.get("source"), "task output source")
                        device_id = _name(source.get("device_id"), "task output source.device_id")
                        point_id = _name(source.get("point_id"), "task output source.point_id")
                        device = devices.get(DeviceId(device_id))
                        if device is None or task.device_group_id not in device.device_group_ids:
                            raise ConfigError(
                                f"Task '{task.task_id}' output references device "
                                f"'{device_id}' outside its device group"
                            )
                        model = models[device.device_model_id]
                        point = point_tables[model.point_table_id].points.get(point_id)
                        if point is None:
                            raise ConfigError(
                                f"Task '{task.task_id}' output references unknown point "
                                f"'{device_id}.{point_id}'"
                            )
                        if task.point_group not in point.point_groups:
                            raise ConfigError(
                                f"Task '{task.task_id}' output point '{device_id}.{point_id}' "
                                "is not collected by this Task (point group mismatch)"
                            )
                    validate_task_sink_points(sink.kind, plain_points)
        validate_core_config(
            units=units,
            device_types=types,
            device_models=models,
            device_groups=groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            tasks=tasks,
            sink_ids=set(sinks),
            sinks=sinks,
        )
        site = Site(site_id=site_id, name=site_name, devices=devices)
        return ConfigSnapshot(
            system=settings,
            units=units,
            site=site,
            device_types=types,
            device_models=models,
            device_groups=groups,
            point_tables=point_tables,
            point_table_baseline=point_tables,
            point_table_definitions=_map(point_document.get("point_tables"), "points.point_tables"),
            business_points=business_points,
            tasks=tasks,
            sinks=sinks,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
