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
        unit = UNIT_CATALOG[UnitCode(_name(entry.get("unit"), f"{context}.unit"))]
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
            for key, value in expanded[parent_id].items()
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
