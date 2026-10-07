"""Shared Core 的规范化 YAML 配置 Codec。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar, cast

import yaml

from core.application.config import (
    CoreConfigArtifact,
    CoreConfigSnapshot,
    validate_core_config,
)
from core.application.errors import ConfigError
from core.application.port import CoreConfigCodecPort
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionEndpoint,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    PointDefinition,
    UNIT_CATALOG,
    Unit,
    UnitCode,
    ValueType,
)

_SCHEMA_VERSION = 3
_MEDIA_TYPE = "application/x-yaml"

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")


class YamlCoreConfigCodec(CoreConfigCodecPort):
    """CoreConfigSnapshot 与单一规范化 YAML 制品之间的转换器。

    YAML 是配置交换格式，不承担运行时继承或隐式默认值解析。decode 后立即构造
    强类型 Shared Core 模型并执行跨对象一致性校验。
    """

    def encode(self, snapshot: CoreConfigSnapshot) -> CoreConfigArtifact:
        """稳定导出配置；相同快照生成确定性字段和对象顺序。"""
        validate_core_config(snapshot)
        payload = _encode_snapshot(snapshot)
        text = yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        return CoreConfigArtifact(content=text.encode("utf-8"), media_type=_MEDIA_TYPE)

    def decode(self, artifact: CoreConfigArtifact) -> CoreConfigSnapshot:
        """解析并校验规范化 YAML 配置制品。"""
        if artifact.media_type not in {_MEDIA_TYPE, "text/yaml", "application/yaml"}:
            raise ConfigError(
                f"unsupported config media type '{artifact.media_type}'"
            )

        try:
            raw = yaml.safe_load(artifact.content.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid YAML config artifact: {exc}") from exc

        root = _require_mapping(raw, "root")
        _require_fields(
            root,
            {
                "schema_version",
                "device_types",
                "device_groups",
                "business_points",
                "point_tables",
                "device_models",
                "devices",
            },
            "root",
        )
        version = root.get("schema_version")
        if type(version) is not int or version != _SCHEMA_VERSION:
            raise ConfigError(
                f"unsupported core config schema_version '{version}', "
                f"expected '{_SCHEMA_VERSION}'"
            )

        try:
            snapshot = _decode_snapshot(root)
            validate_core_config(snapshot)
        except ConfigError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid core config artifact: {exc}") from exc
        return snapshot


def _encode_snapshot(snapshot: CoreConfigSnapshot) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "device_types": [
            {
                "device_type_id": str(item.device_type_id),
                "name": item.name,
                "description": item.description,
            }
            for item in _sorted_values(snapshot.device_types)
        ],
        "device_groups": [
            {
                "device_group_id": str(item.device_group_id),
                "name": item.name,
                "description": item.description,
            }
            for item in _sorted_values(snapshot.device_groups)
        ],
        "business_points": [
            {
                "business_point_id": str(item.business_point_id),
                "value_type": item.value_type.value,
                "standard_unit": item.standard_unit.code.value,
                "description": item.description,
            }
            for item in _sorted_values(snapshot.business_points)
        ],
        "point_tables": [
            {
                "point_table_id": str(table.point_table_id),
                "protocol": table.protocol.name,
                "points": [
                    {
                        "point_id": point.point_id,
                        "business_point_id": str(point.business_point_id),
                        "source_unit": point.source_unit.code.value,
                        "access": point.access.value,
                        "scale": point.scale,
                        "offset": point.offset,
                        "protocol_options": dict(
                            snapshot.point_options_for(
                                table.point_table_id,
                                point.point_id,
                            )
                        ),
                    }
                    for point in sorted(
                        table.points.values(),
                        key=lambda value: value.point_id,
                    )
                ],
            }
            for table in _sorted_values(snapshot.point_tables)
        ],
        "device_models": [
            {
                "device_model_id": str(item.device_model_id),
                "device_type_id": str(item.device_type_id),
                "point_table_id": str(item.point_table_id),
                "name": item.name,
                "manufacturer": item.manufacturer,
            }
            for item in _sorted_values(snapshot.device_models)
        ],
        "devices": [
            {
                "device_id": str(item.device_id),
                "device_model_id": str(item.device_model_id),
                "name": item.name,
                "device_group_ids": [str(value) for value in item.device_group_ids],
                "endpoint": {
                    "host": item.endpoint.host,
                    "port": item.endpoint.port,
                    "options": dict(snapshot.device_options_for(item.device_id)),
                },
            }
            for item in _sorted_values(snapshot.devices)
        ],
    }


def _decode_snapshot(root: Mapping[str, object]) -> CoreConfigSnapshot:
    device_types = {
        item.device_type_id: item
        for item in (
            _decode_device_type(value)
            for value in _require_list(root.get("device_types"), "device_types")
        )
    }
    device_groups = {
        item.device_group_id: item
        for item in (
            _decode_device_group(value)
            for value in _require_list(root.get("device_groups"), "device_groups")
        )
    }
    business_points = {
        item.business_point_id: item
        for item in (
            _decode_business_point(value)
            for value in _require_list(root.get("business_points"), "business_points")
        )
    }
    decoded_point_tables = [
        _decode_point_table(value)
        for value in _require_list(
            root.get("point_tables"),
            "point_tables",
        )
    ]
    point_tables = {
        table.point_table_id: table
        for table, _ in decoded_point_tables
    }
    point_options = {
        table.point_table_id: options
        for table, options in decoded_point_tables
    }
    device_models = {
        item.device_model_id: item
        for item in (
            _decode_device_model(value)
            for value in _require_list(
                root.get("device_models"),
                "device_models",
            )
        )
    }
    decoded_devices = [
        _decode_device(value)
        for value in _require_list(root.get("devices"), "devices")
    ]
    devices = {
        device.device_id: device
        for device, _ in decoded_devices
    }
    device_options = {
        device.device_id: options
        for device, options in decoded_devices
    }

    _require_unique_count(root, "device_types", len(device_types))
    _require_unique_count(root, "device_groups", len(device_groups))
    _require_unique_count(root, "business_points", len(business_points))
    _require_unique_count(root, "point_tables", len(point_tables))
    _require_unique_count(root, "device_models", len(device_models))
    _require_unique_count(root, "devices", len(devices))

    return CoreConfigSnapshot(
        device_types=device_types,
        device_models=device_models,
        device_groups=device_groups,
        devices=devices,
        business_points=business_points,
        point_tables=point_tables,
        device_options=device_options,
        point_options=point_options,
    )


def _decode_device_type(value: object) -> DeviceType:
    item = _require_mapping(value, "device_types[]")
    _require_fields(
        item,
        {"device_type_id", "name", "description"},
        "device_types[]",
    )
    return DeviceType(
        device_type_id=DeviceTypeId(_required_str(item, "device_type_id")),
        name=_required_str(item, "name"),
        description=_optional_str(item, "description"),
    )


def _decode_device_group(value: object) -> DeviceGroup:
    item = _require_mapping(value, "device_groups[]")
    _require_fields(
        item,
        {"device_group_id", "name", "description"},
        "device_groups[]",
    )
    return DeviceGroup(
        device_group_id=DeviceGroupId(_required_str(item, "device_group_id")),
        name=_required_str(item, "name"),
        description=_optional_str(item, "description"),
    )


def _decode_business_point(value: object) -> BusinessPoint:
    item = _require_mapping(value, "business_points[]")
    _require_fields(
        item,
        {"business_point_id", "value_type", "standard_unit", "description"},
        "business_points[]",
    )
    return BusinessPoint(
        business_point_id=BusinessPointId(_required_str(item, "business_point_id")),
        value_type=ValueType(_required_str(item, "value_type")),
        standard_unit=_unit(_required_str(item, "standard_unit")),
        description=_optional_str(item, "description"),
    )


def _decode_point_table(
    value: object,
) -> tuple[
    PointTable,
    dict[str, dict[str, str | int | float | bool | None]],
]:
    item = _require_mapping(value, "point_tables[]")
    _require_fields(
        item,
        {"point_table_id", "protocol", "points"},
        "point_tables[]",
    )
    decoded_points = [
        _decode_protocol_point(raw)
        for raw in _require_list(item.get("points"), "points")
    ]
    points = [point for point, _ in decoded_points]
    by_id = {point.point_id: point for point in points}
    if len(by_id) != len(points):
        raise ConfigError(
            f"point table '{_required_str(item, 'point_table_id')}' contains duplicate point_id"
        )
    table = PointTable(
        point_table_id=PointTableId(_required_str(item, "point_table_id")),
        protocol=Protocol(_required_str(item, "protocol")),
        points=by_id,
    )
    return (
        table,
        {
            point.point_id: options
            for point, options in decoded_points
        },
    )


def _decode_protocol_point(
    value: object,
) -> tuple[
    PointDefinition,
    dict[str, str | int | float | bool | None],
]:
    item = _require_mapping(value, "points[]")
    _require_fields(
        item,
        {
            "point_id",
            "business_point_id",
            "source_unit",
            "access",
            "scale",
            "offset",
            "protocol_options",
        },
        "points[]",
    )
    point = PointDefinition(
        point_id=_required_str(item, "point_id"),
        business_point_id=BusinessPointId(
            _required_str(item, "business_point_id")
        ),
        source_unit=_unit(_required_str(item, "source_unit")),
        access=PointAccess(_required_str(item, "access")),
        scale=_number(item, "scale", default=1.0),
        offset=_number(item, "offset", default=0.0),
    )
    return (
        point,
        _scalar_mapping(
            item.get("protocol_options"),
            "protocol_options",
        ),
    )


def _decode_device_model(value: object) -> DeviceModel:
    item = _require_mapping(value, "device_models[]")
    _require_fields(
        item,
        {
            "device_model_id",
            "device_type_id",
            "point_table_id",
            "name",
            "manufacturer",
        },
        "device_models[]",
    )
    return DeviceModel(
        device_model_id=DeviceModelId(_required_str(item, "device_model_id")),
        device_type_id=DeviceTypeId(_required_str(item, "device_type_id")),
        point_table_id=PointTableId(
            _required_str(item, "point_table_id")
        ),
        name=_optional_str(item, "name"),
        manufacturer=_optional_str(item, "manufacturer"),
    )


def _decode_device(
    value: object,
) -> tuple[
    Device,
    dict[str, str | int | float | bool | None],
]:
    item = _require_mapping(value, "devices[]")
    _require_fields(
        item,
        {"device_id", "device_model_id", "name", "device_group_ids", "endpoint"},
        "devices[]",
    )
    endpoint = _require_mapping(item.get("endpoint"), "endpoint")
    _require_fields(endpoint, {"host", "port", "options"}, "endpoint")
    device = Device(
        device_id=DeviceId(_required_str(item, "device_id")),
        device_model_id=DeviceModelId(_required_str(item, "device_model_id")),
        endpoint=ConnectionEndpoint(
            host=_required_str(endpoint, "host"),
            port=_optional_int(endpoint, "port"),
        ),
        name=_optional_str(item, "name"),
        device_group_ids=tuple(
            DeviceGroupId(_require_str(value, "device_group_ids[]"))
            for value in _require_list(item.get("device_group_ids"), "device_group_ids")
        ),
    )
    return (
        device,
        _scalar_mapping(
            endpoint.get("options"),
            "endpoint.options",
        ),
    )


def _unit(value: str) -> Unit:
    try:
        return UNIT_CATALOG[UnitCode(value)]
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"unknown built-in unit '{value}'") from exc


def _sorted_values(
    values: Mapping[_KeyT, _ValueT],
) -> list[_ValueT]:
    return [values[key] for key in sorted(values, key=str)]


def _require_unique_count(
    root: Mapping[str, object],
    field: str,
    actual_count: int,
) -> None:
    raw = _require_list(root.get(field), field)
    if len(raw) != actual_count:
        raise ConfigError(f"{field} contains duplicate identities")


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{field} keys must be strings")
    return cast(Mapping[str, object], value)


def _require_list(value: object, field: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    return value


def _required_str(item: Mapping[str, object], field: str) -> str:
    if field not in item:
        raise ConfigError(f"{field} is required")
    return _require_str(item[field], field)


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_str(item: Mapping[str, object], field: str) -> str | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a string or null")
    value = value.strip()
    return value or None


def _optional_int(item: Mapping[str, object], field: str) -> int | None:
    value = item.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer or null")
    return value


def _number(
    item: Mapping[str, object],
    field: str,
    *,
    default: float,
) -> float:
    value = item.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{field} must be numeric")
    return float(value)


def _scalar_mapping(value: object, field: str) -> dict[str, str | int | float | bool | None]:
    if value is None:
        return {}
    mapping = _require_mapping(value, field)
    result: dict[str, str | int | float | bool | None] = {}
    for key, item in mapping.items():
        if item is not None and not isinstance(item, str | int | float | bool):
            raise ConfigError(f"{field}.{key} must be a scalar value")
        result[key] = item
    return result


def _require_fields(
    item: Mapping[str, object],
    allowed: set[str],
    context: str,
) -> None:
    """拒绝未知字段，避免配置拼写错误被静默忽略。"""
    unknown = set(item) - allowed
    if unknown:
        raise ConfigError(
            f"{context} contains unknown fields: {sorted(unknown)}"
        )


