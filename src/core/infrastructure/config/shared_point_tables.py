"""Shared typed point-table parsing and inheritance resolution."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from core.application.errors import ConfigError
from core.domain import (
    BusinessPointId,
    DataType,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.domain.unit import DIMENSIONLESS, UnitCatalog

_PROTOCOLS = frozenset({"ads", "modbus", "iec104"})

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


_ADS_LOGICAL_TYPES: dict[str, str] = {
    "BOOL": "bool",
    "SINT": "int8",
    "USINT": "uint8",
    "INT": "int16",
    "UINT": "uint16",
    "DINT": "int32",
    "UDINT": "uint32",
    "LINT": "int64",
    "ULINT": "uint64",
    "REAL": "float32",
    "LREAL": "float64",
    "STRING": "str",
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
    identity = BusinessPointId(_name(raw_business_id, f"{context}.business_point_id"))
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
        parent = resolve(_name(parent_id, f"{name}.parent_id")) if parent_id is not None else None
        protocol = definition.get("protocol", parent.protocol.name if parent else None)
        if protocol not in _PROTOCOLS:
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
