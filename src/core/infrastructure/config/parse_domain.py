"""YAML -> Core Domain 的直接解析。无 Config VO 中间模型。"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.settings import ADSLocalConfig, RuntimeSettings, SystemSettings
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
    Site,
    Sink,
    Task,
    validate_core_config,
)
from core.domain.unit import UNIT_CATALOG, UnitCode
from core.infrastructure.protocol.ads.config import is_valid_ams_net_id, parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config
from .business_points import parse_business_points
from .snapshot_writer import _yaml_plain
from .yaml import read_yaml_mapping


_OPTION_PARSERS = {
    "ads": parse_ads_config,
    "modbus": parse_modbus_config,
    "iec104": parse_iec104_config,
}
_POINT_FIELDS = {
    "point_id", "business_point_id", "variable_name", "point_groups",
    "address", "data_type", "scale", "offset", "unit", "description",
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


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ConfigError(f"{context} must be a finite number")
    return float(value)


def _flag(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be boolean")
    return value


def _system(raw: Mapping[str, Any]) -> tuple[str, str, SystemSettings]:
    site = _map(raw.get("site"), "system.site")
    _keys(site, {"site_id", "name"}, "system.site")
    site_id = _name(site.get("site_id"), "system.site_id")
    name = _name(site.get("name", site_id), "system.site.name")
    runtime_raw = _map(raw.get("runtime", {}), "system.runtime")
    _keys(runtime_raw, set(RuntimeSettings.__dataclass_fields__), "system.runtime")
    settings = dict(runtime_raw)
    for key in ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout", "retry_interval"):
        if key in settings and settings[key] is not None:
            settings[key] = _number(settings[key], f"runtime.{key}")
    if "queue_maxsize" in settings and settings["queue_maxsize"] is not None:
        value = settings["queue_maxsize"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("runtime.queue_maxsize must be an integer")
    if "read_retries" in settings:
        value = settings["read_retries"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("runtime.read_retries must be an integer")
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
            username=_name(values.get("username", "Administrator"), "ads.username"),
            password=values.get("password", ""),
        )
    return site_id, name, SystemSettings(runtime=runtime, ads=ads)


def _point(
    point_id: str,
    entry: Mapping[str, Any],
    protocol: str,
    business_points: Mapping[BusinessPointId, Any],
) -> Point:
    context = f"point '{point_id}'"
    _keys(entry, _POINT_FIELDS, context)
    identity = BusinessPointId(_name(entry.get("business_point_id"), f"{context}.business_point_id"))
    business_point = business_points.get(identity)
    if business_point is None:
        raise ConfigError(f"{context} unknown business point '{identity}'")
    try:
        data_type = DataType(_name(entry.get("data_type"), f"{context}.data_type"))
        unit = UNIT_CATALOG[UnitCode(_name(entry.get("unit", "none"), f"{context}.unit"))]
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"{context}: invalid data type or unit: {exc}") from exc
    if data_type != business_point.data_type or unit.quantity != business_point.standard_unit.quantity:
        raise ConfigError(f"{context}: incompatible business point '{identity}'")
    scale = _number(entry.get("scale", 1.0), f"{context}.scale")
    offset = _number(entry.get("offset", 0.0), f"{context}.offset")
    if data_type in (DataType.BOOL, DataType.STRING):
        if unit.code != UnitCode.NONE or scale != 1.0 or offset != 0.0:
            raise ConfigError(f"{context}: bool/string must be dimensionless without scaling")

    address = _map(entry.get("address"), f"{context}.address")
    extensions: dict[str, Any] = {}
    for key, value in address.items():
        if value is None or not isinstance(value, (str, int, float, bool)):
            raise ConfigError(f"{context}.address.{key} must be a scalar")
        if isinstance(value, float) and not isfinite(value):
            raise ConfigError(f"{context}.address.{key} must be finite")
        extensions[key] = value
    extensions["data_type"] = data_type.value
    groups = tuple(_name(name, f"{context}.point_groups") for name in _list(
        entry.get("point_groups", []), f"{context}.point_groups"
    ))
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
        variable_name=entry.get("variable_name"),
        point_groups=groups,
    )


def _point_tables(
    raw: Mapping[str, Any],
    business_points: Mapping[BusinessPointId, Any],
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
        _keys(definition, {"protocol", "extends", "remove_points", "points"}, f"point table '{name}'")
        parent_id = definition.get("extends")
        parent = resolve(_name(parent_id, f"{name}.extends")) if parent_id is not None else None
        protocol = definition.get("protocol", parent.protocol.name if parent else None)
        if protocol not in _OPTION_PARSERS:
            raise ConfigError(f"point table '{name}' unsupported protocol '{protocol}'")
        if parent is not None and parent.protocol.name != protocol:
            raise ConfigError(f"point table '{name}' protocol differs from parent")
        points = {
            key: dict(value)
            for key, value in expanded[str(parent_id)].items()
        } if parent is not None else {}

        remove = _list(definition.get("remove_points", []), f"{name}.remove_points")
        if len(remove) != len(set(remove)):
            raise ConfigError(f"point table '{name}' duplicate remove_points")
        for key in remove:
            if key not in points:
                raise ConfigError(f"point table '{name}' cannot remove unknown point '{key}'")
            del points[key]
        patches = _list(definition.get("points", []), f"{name}.points")
        seen: set[str] = set()
        for item in patches:
            patch = _map(item, f"{name}.point")
            _keys(patch, _POINT_FIELDS, f"{name}.point")
            key = _name(patch.get("point_id"), "point.point_id")
            if key in seen:
                raise ConfigError(f"point table '{name}' duplicate point '{key}'")
            seen.add(key)
            updated = dict(points.get(key, {}))
            updated.update({k: v for k, v in patch.items() if k != "point_id"})
            points[key] = updated
        domain_points = {
            key: _point(key, value, protocol, business_points)
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

    for key in definitions:
        resolve(_name(key, "point table ID"))
    return {PointTableId(key): table for key, table in cache.items()}


def _device_catalog(
    model_doc: Mapping[str, Any],
    device_doc: Mapping[str, Any],
    tables: Mapping[PointTableId, PointTable],
) -> tuple[
    dict[DeviceTypeId, DeviceType],
    dict[DeviceModelId, DeviceModel],
    dict[DeviceGroupId, DeviceGroup],
    dict[DeviceId, Device],
    dict[DeviceId, dict[str, Any]],
    frozenset[DeviceId],
]:
    _keys(model_doc, {"device_types", "device_models"}, "device_models.yaml")
    types: dict[DeviceTypeId, DeviceType] = {}
    for key, item in _map(model_doc.get("device_types", {}), "device_types").items():
        definition = _map(item, f"device type '{key}'")
        _keys(definition, {"name", "description"}, f"device type '{key}'")
        type_identity = DeviceTypeId(_name(key, "device type"))
        types[type_identity] = DeviceType(
            device_type_id=type_identity,
            name=_name(definition.get("name", key), f"device type '{key}'.name"),
            description=definition.get("description"),
        )

    raw_models = _map(model_doc.get("device_models", {}), "device_models")
    models: dict[DeviceModelId, DeviceModel] = {}
    for key, item in raw_models.items():
        definition = _map(item, f"device model '{key}'")
        _keys(definition, {
            "device_type", "manufacturer", "model", "protocol", "point_table",
            "read_mode", "properties", "connection_defaults",
        }, f"device model '{key}'")
        model_identity = DeviceModelId(_name(key, "device model ID"))
        type_id = DeviceTypeId(_name(definition.get("device_type"), "device_type"))
        table_id = PointTableId(_name(definition.get("point_table"), "point_table"))
        protocol = _name(definition.get("protocol"), "model.protocol")
        if type_id not in types or table_id not in tables:
            raise ConfigError(f"device model '{key}' references unknown type or point table")
        if tables[table_id].protocol.name != protocol:
            raise ConfigError(f"device model '{key}' protocol does not match point table")
        if protocol not in _OPTION_PARSERS:
            raise ConfigError(f"unsupported protocol '{protocol}'")
        if protocol != "ads" and definition.get("read_mode") is not None:
            raise ConfigError(f"device model '{key}' read_mode only supported for ADS")
        if protocol == "ads" and (definition.get("read_mode") or "sum") != "sum":
            raise ConfigError(f"device model '{key}' only supports ADS sum mode")
        _map(definition.get("connection_defaults", {}), f"model '{key}'.connection_defaults")
        _map(definition.get("properties", {}), f"model '{key}'.properties")
        models[model_identity] = DeviceModel(
            device_model_id=model_identity,
            device_type_id=type_id,
            point_table_id=table_id,
            name=definition.get("model"),
            manufacturer=definition.get("manufacturer"),
            read_mode=definition.get("read_mode"),
            properties=definition.get("properties") or {},
            connection_defaults=definition.get("connection_defaults") or {},
        )

    _keys(device_doc, {"devices", "device_groups"}, "devices.yaml")
    groups: dict[DeviceGroupId, DeviceGroup] = {}
    devices: dict[DeviceId, Device] = {}
    options_by_device: dict[DeviceId, dict[str, Any]] = {}
    ads_subscriptions: set[DeviceId] = set()
    for item in _list(device_doc.get("devices"), "devices"):
        definition = _map(item, "devices entry")
        _keys(definition, {
            "device_id", "model", "device_group", "device_groups", "endpoint",
            "enabled", "name",
        }, "device")
        device_identity = DeviceId(_name(definition.get("device_id"), "device_id"))
        if device_identity in devices:
            raise ConfigError(f"duplicate device_id '{device_identity}'")
        model_id = DeviceModelId(_name(definition.get("model"), "device.model"))
        if model_id not in models:
            raise ConfigError(f"device '{device_identity}' unknown model '{model_id}'")
        model = _map(raw_models[model_id], f"device model '{model_id}'")
        protocol = _name(model.get("protocol"), "protocol")
        raw_groups = definition.get("device_groups")
        if raw_groups is not None and definition.get("device_group") is not None:
            raise ConfigError(f"device '{device_identity}' cannot specify both group formats")
        groups_list = raw_groups if raw_groups is not None else [definition.get("device_group")]
        memberships = tuple(
            DeviceGroupId(_name(group, f"device '{device_identity}'.group"))
            for group in _list(groups_list, "device_groups")
        )
        for group in memberships:
            groups.setdefault(group, DeviceGroup(device_group_id=group, name=str(group)))

        raw_endpoint = _map(definition.get("endpoint"), f"device '{device_identity}'.endpoint")
        _keys(raw_endpoint, {"host", "port", "extensions"}, f"device '{device_identity}'.endpoint")
        defaults = dict(_map(model.get("connection_defaults", {}), "connection_defaults"))
        port = raw_endpoint.get("port", defaults.pop("port", None))
        defaults.pop("port", None)
        if port is None:
            raise ConfigError(f"device '{device_identity}' requires endpoint port or model default")
        if isinstance(port, bool) or not isinstance(port, (int, str)):
            raise ConfigError(f"device '{device_identity}' invalid port")
        if isinstance(port, str) and not port.isdecimal():
            raise ConfigError(f"device '{device_identity}' port must be numeric")
        endpoint = ConnectionEndpoint(
            host=_name(raw_endpoint.get("host"), f"device '{device_identity}'.host"),
            port=int(port),
        )
        extensions = _map(raw_endpoint.get("extensions", {}), "endpoint.extensions")
        options = {**defaults, **extensions}
        if protocol == "ads":
            options["read_mode"] = model.get("read_mode", "sum") or "sum"
            if options.pop("subscribe_enabled", False):
                ads_subscriptions.add(device_identity)
        for key, value in options.items():
            if not isinstance(key, str) or value is not None and not isinstance(
                value, (str, int, float, bool)
            ):
                raise ConfigError(f"device '{device_identity}' invalid protocol option '{key}'")
        try:
            _OPTION_PARSERS[protocol](endpoint, options)
        except Exception as exc:
            raise ConfigError(f"device '{device_identity}' invalid protocol options: {exc}") from exc
        devices[device_identity] = Device(
            device_id=device_identity,
            device_model_id=model_id,
            endpoint=endpoint,
            name=definition.get("name"),
            device_group_ids=memberships,
            enabled=_flag(definition.get("enabled", True), "device.enabled"),
        )
        options_by_device[device_identity] = options

    declared_groups = _map(device_doc.get("device_groups", {}), "devices.device_groups")
    for key, item in declared_groups.items():
        group_identity = DeviceGroupId(_name(key, "device group"))
        data = _map(item, f"device group '{key}'")
        _keys(data, {"name", "description"}, f"device group '{key}'")
        groups[group_identity] = DeviceGroup(
            device_group_id=group_identity,
            name=_name(data.get("name", key), f"device group '{key}'.name"),
            description=data.get("description"),
        )
    return types, models, groups, devices, options_by_device, frozenset(ads_subscriptions)


def _tasks(raw: Mapping[str, Any]) -> dict[str, Task]:
    _keys(raw, {"tasks"}, "tasks.yaml")
    result: dict[str, Task] = {}
    for entry in _list(raw.get("tasks", []), "tasks.yaml"):
        data = _map(entry, "task")
        _keys(data, {
            "task_id", "device_group", "point_group", "interval", "targets", "enabled",
        }, "task")
        identity = _name(data.get("task_id"), "task.task_id")
        if identity in result:
            raise ConfigError(f"duplicate task '{identity}'")
        targets = []
        for target in _list(data.get("targets"), f"task '{identity}'.targets"):
            mapping = _map(target, f"task '{identity}'.target")
            _keys(mapping, {"sink"}, f"task '{identity}'.target")
            targets.append(_name(mapping.get("sink"), "target.sink"))
        result[identity] = Task(
            task_id=identity,
            device_group_id=DeviceGroupId(_name(data.get("device_group"), "task.device_group")),
            point_group=_name(data.get("point_group"), "task.point_group"),
            sink_ids=tuple(targets),
            interval=_number(data.get("interval"), "task.interval"),
            enabled=_flag(data.get("enabled", True), "task.enabled"),
        )
    return result


def _sinks(raw: Mapping[str, Any]) -> dict[str, Sink]:
    from .sink_schema import _SinksSchema

    try:
        definitions = _SinksSchema.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"invalid sinks.yaml: {exc}") from exc
    sinks: dict[str, Sink] = {}
    for definition in definitions.sinks:
        entry = _yaml_plain(definition.model_dump(mode="python", by_alias=True))
        sinks[definition.name] = Sink(
            sink_id=definition.name,
            kind=definition.type,
            enabled=definition.enabled,
            connection=entry["connection"],
            points=tuple(entry.get("points", ())),
        )
    return sinks


def load_domain(config_dir: Path) -> ConfigSnapshot:
    """一次性解析完整 YAML 集合，构建并验证领域快照。"""
    base = Path(config_dir)
    system = read_yaml_mapping(base / "system.yaml")
    model_document = read_yaml_mapping(base / "device_models.yaml")
    device_document = read_yaml_mapping(base / "devices.yaml")
    point_document = read_yaml_mapping(base / "points.yaml")
    task_document = read_yaml_mapping(base / "tasks.yaml")
    sink_document = read_yaml_mapping(base / "sinks.yaml")
    business_document = read_yaml_mapping(base / "business_points.yaml")
    try:
        site_id, site_name, settings = _system(system)
        business_points = parse_business_points(business_document)
        point_tables = _point_tables(point_document, business_points)
        types, models, groups, devices, options, ads_subscriptions = _device_catalog(
            model_document, device_document, point_tables,
        )
        tasks = _tasks(task_document)
        sinks = _sinks(sink_document)
        validate_core_config(
            device_types=types,
            device_models=models,
            device_groups=groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            protocol_options_by_device=options,
            tasks=tasks,
            sink_ids=set(sinks),
            sinks=sinks,
        )
        site = Site(site_id=site_id, name=site_name, devices=devices)
        return ConfigSnapshot(
            system=settings,
            site=site,
            device_types=types,
            device_models=models,
            device_groups=groups,
            point_tables=point_tables,
            business_points=business_points,
            tasks=tasks,
            sinks=sinks,
            protocol_options_by_device=options,
            ads_subscribe_devices=ads_subscriptions,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
