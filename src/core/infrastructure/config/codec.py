"""YAML 原始 Mapping ↔ 配置 VO 的严格编解码。

本模块是唯一的通用配置编解码层：不再维护与 Domain Config 平行的
Raw/Definition 模型层，而是用明确的字段检查函数把 YAML 原始 Mapping
严格解析为 ``core.domain.config`` 的值对象，并把值对象序列化回标准
YAML 结构（供原子保存）。

严格性契约（与旧 Raw Schema 一致）：

- 未知字段一律报错，不允许被默默忽略；
- 布尔值不会被接受为整数/浮点数；
- 缺失字段按既定默认值处理，非法类型/取值不会流入 Domain；
- 浮点值必须有限（拒绝 NaN/Inf）；
- 点表继承输入使用 :class:`PointPatch` 区分「未声明」与「显式 null」，
  它只是解释未展开配置的输入结构，不是第二套配置模型。

VO 构造不变量抛 :class:`ValueError`，本层统一转换为 :class:`ConfigError`。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from core.application.errors import ConfigError
from core.application.sink_config import SinksConfig
from core.domain.config import (
    ADS_READ_MODES,
    SUPPORTED_PROTOCOLS,
    ADSLocalConfig,
    DeviceInstanceConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    EndpointConfig,
    PointTableConfig,
    PointTablesConfig,
    RuntimeSettings,
    SystemConfig,
    TaskConfig,
    TasksConfig,
    UnitDefinitionConfig,
    UnitsConfig,
)
from core.infrastructure.protocol.ads.config import is_valid_ams_net_id

from .point_tables import PointPatch, PointTableDraft, resolve_point_tables

# ---------------------------------------------------------------------------
# 基础字段检查
# ---------------------------------------------------------------------------


def _expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{label} has unknown keys: {sorted(unknown)}")


def _required(raw: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{label}: missing required key '{key}'")
    return raw[key]


def _as_str(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _opt_str(raw: Mapping[str, Any], key: str, label: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    return _as_str(value, f"{label}.{key}")


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean, got {type(value).__name__}")
    return value


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer, got {type(value).__name__}")
    return int(value)


def _as_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label} must be a number, got {type(value).__name__}")
    if not isfinite(value):
        raise ConfigError(f"{label} must be finite, got {value}")
    return float(value)


def _as_str_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a list, got {type(value).__name__}")
    return [_as_str(item, f"{label} item") for item in value]


def _vo_error(context: str, exc: ValueError) -> ConfigError:
    return ConfigError(f"{context}: {exc}")


# ---------------------------------------------------------------------------
# system.yaml
# ---------------------------------------------------------------------------

_SITE_KEYS = {"site_id", "name"}
_RUNTIME_KEYS = {
    "queue_maxsize",
    "backpressure_policy",
    "shutdown_timeout",
    "connect_timeout",
    "read_timeout",
    "write_timeout",
    "read_retries",
    "retry_interval",
}
_ADS_KEYS = {"local_ams_net_id", "local_ip", "username", "password"}


def parse_system_config(raw: Mapping[str, Any]) -> SystemConfig:
    """解析 system.yaml 共享段；进程专属的未知顶层段保持忽略（旧行为）。"""
    site_id, site_name = _parse_site(raw.get("site"))
    try:
        return SystemConfig(
            site_id=site_id,
            site_name=site_name,
            runtime=_parse_runtime(raw.get("runtime")),
            ads=_parse_ads(raw.get("ads")),
        )
    except ValueError as exc:
        raise _vo_error("Invalid system configuration", exc) from exc


def _parse_site(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    raw = _expect_mapping(value, "system.yaml 'site'")
    _reject_unknown(raw, _SITE_KEYS, "system.yaml 'site'")
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise ConfigError("system.yaml 'site.name' must be a string")
    site_id = raw.get("site_id")
    if site_id is None:
        if name is not None:
            raise ConfigError("system.yaml 'site.name' requires 'site.site_id'")
        return None, None
    return _as_str(site_id, "system.yaml 'site.site_id'"), name


def _parse_runtime(value: Any) -> RuntimeSettings:
    if value is None:
        return RuntimeSettings()
    raw = _expect_mapping(value, "system.yaml 'runtime'")
    _reject_unknown(raw, _RUNTIME_KEYS, "system.yaml 'runtime'")

    queue_maxsize = raw.get("queue_maxsize")
    policy = raw.get("backpressure_policy")
    if policy is not None:
        policy = _as_str(policy, "system.yaml 'runtime.backpressure_policy'")
    timeouts: dict[str, float | None] = {}
    for key in ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout"):
        item = raw.get(key)
        timeouts[key] = None if item is None else _as_float(item, f"system.yaml 'runtime.{key}'")
    try:
        return RuntimeSettings(
            queue_maxsize=(
                None
                if queue_maxsize is None
                else _as_int(queue_maxsize, "system.yaml 'runtime.queue_maxsize'")
            ),
            backpressure_policy=policy,
            shutdown_timeout=timeouts["shutdown_timeout"],
            connect_timeout=timeouts["connect_timeout"],
            read_timeout=timeouts["read_timeout"],
            write_timeout=timeouts["write_timeout"],
            read_retries=_as_int(
                raw.get("read_retries", 1),
                "system.yaml 'runtime.read_retries'",
            ),
            retry_interval=_as_float(
                raw.get("retry_interval", 1.0),
                "system.yaml 'runtime.retry_interval'",
            ),
        )
    except ValueError as exc:
        raise _vo_error("Invalid system.runtime configuration", exc) from exc


def _parse_ads(value: Any) -> ADSLocalConfig | None:
    if value is None:
        return None
    raw = _expect_mapping(value, "system.yaml 'ads'")
    _reject_unknown(raw, _ADS_KEYS, "system.yaml 'ads'")
    net_id = _as_str(
        _required(raw, "local_ams_net_id", "system.yaml 'ads'"), "ads.local_ams_net_id"
    )
    if not is_valid_ams_net_id(net_id):
        raise ConfigError(
            f"Invalid AMS Net ID '{net_id}'; expected dotted-numeric 'a.b.c.d.e.f'"
        )
    username = raw.get("username", "Administrator")
    password = raw.get("password", "")
    try:
        return ADSLocalConfig(
            local_ams_net_id=net_id,
            local_ip=_as_str(
                _required(raw, "local_ip", "system.yaml 'ads'"), "ads.local_ip"
            ),
            username=_as_str(username, "ads.username", allow_empty=False),
            password=_as_str(password, "ads.password", allow_empty=True),
        )
    except ValueError as exc:
        raise _vo_error("Invalid system.ads configuration", exc) from exc


def dump_system_config(config: SystemConfig) -> dict[str, Any]:
    """把 SystemConfig 序列化为标准 YAML 结构（None 字段省略）。"""
    data: dict[str, Any] = {}
    if config.site_id is not None:
        site: dict[str, Any] = {"site_id": config.site_id}
        if config.site_name is not None:
            site["name"] = config.site_name
        data["site"] = site
    runtime: dict[str, Any] = {}
    for key in (
        "queue_maxsize",
        "backpressure_policy",
        "shutdown_timeout",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "read_retries",
        "retry_interval",
    ):
        value = getattr(config.runtime, key)
        default = {"read_retries": 1, "retry_interval": 1.0}.get(key)
        if value is not None and value != default:
            runtime[key] = value
    if runtime:
        data["runtime"] = runtime
    if config.ads is not None:
        data["ads"] = {
            "local_ams_net_id": config.ads.local_ams_net_id,
            "local_ip": config.ads.local_ip,
            "username": config.ads.username,
            "password": config.ads.password,
        }
    return data


# ---------------------------------------------------------------------------
# device_models.yaml
# ---------------------------------------------------------------------------

_DEVICE_TYPE_KEYS = {"name"}
_DEVICE_MODEL_KEYS = {
    "device_type",
    "manufacturer",
    "model",
    "protocol",
    "point_table",
    "read_mode",
    "properties",
    "connection_defaults",
}


def parse_device_models_config(raw: Mapping[str, Any]) -> DeviceModelsConfig:
    _reject_unknown(raw, {"device_types", "device_models"}, "device_models.yaml")
    types_raw = _expect_mapping(
        raw.get("device_types") or {}, "device_models.yaml 'device_types'"
    )
    models_raw = _expect_mapping(
        raw.get("device_models") or {}, "device_models.yaml 'device_models'"
    )

    device_types: dict[str, str | None] = {}
    for type_id, item in types_raw.items():
        label = f"device_type '{type_id}'"
        item = _expect_mapping(item, label)
        _reject_unknown(item, _DEVICE_TYPE_KEYS, label)
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"{label}.name must be a string")
        device_types[_as_str(type_id, "device_type id")] = name

    device_models: dict[str, DeviceModelConfig] = {}
    for model_id, item in models_raw.items():
        label = f"device_model '{model_id}'"
        item = _expect_mapping(item, label)
        _reject_unknown(item, _DEVICE_MODEL_KEYS, label)
        protocol = _as_str(_required(item, "protocol", label), f"{label}.protocol")
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigError(
                f"Device model: protocol '{protocol}' must be one of "
                f"{sorted(SUPPORTED_PROTOCOLS)}"
            )
        try:
            device_models[_as_str(model_id, "device_model id")] = DeviceModelConfig(
                device_type=_as_str(
                    _required(item, "device_type", label), f"{label}.device_type"
                ),
                manufacturer=_opt_str(item, "manufacturer", label),
                model=_opt_str(item, "model", label),
                protocol=protocol,
                point_table=_as_str(
                    _required(item, "point_table", label), f"{label}.point_table"
                ),
                read_mode=_opt_str(item, "read_mode", label),
                properties=_expect_mapping(item.get("properties") or {}, f"{label}.properties"),
                connection_defaults=_expect_mapping(
                    item.get("connection_defaults") or {}, f"{label}.connection_defaults"
                ),
            )
        except ValueError as exc:
            raise _vo_error(f"Invalid {label}", exc) from exc

    for model_id, model in device_models.items():
        if model.device_type not in device_types:
            raise ConfigError(
                f"Device model '{model_id}' references unknown device_type "
                f"'{model.device_type}' (available: {sorted(device_types)})"
            )
    return DeviceModelsConfig(device_types=device_types, device_models=device_models)


def dump_device_models_config(config: DeviceModelsConfig) -> dict[str, Any]:
    return {
        "device_types": {
            type_id: ({"name": name} if name is not None else {})
            for type_id, name in config.device_types.items()
        },
        "device_models": {
            model_id: _dump_device_model(model) for model_id, model in config.device_models.items()
        },
    }


def _dump_device_model(model: DeviceModelConfig) -> dict[str, Any]:
    data: dict[str, Any] = {
        "device_type": model.device_type,
        "manufacturer": model.manufacturer,
        "model": model.model,
        "protocol": model.protocol,
        "point_table": model.point_table,
        "read_mode": model.read_mode,
        "properties": _plain(model.properties),
        "connection_defaults": _plain(model.connection_defaults),
    }
    return data


def _plain(value: Any) -> Any:
    """解冻 MappingProxyType/tuple 为普通 dict/list，供 YAML 序列化。"""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# devices.yaml
# ---------------------------------------------------------------------------

_DEVICE_KEYS = {"device_id", "model", "device_group", "endpoint", "enabled"}
_ENDPOINT_KEYS = {"host", "port", "extensions"}


def parse_devices_config(raw: Mapping[str, Any]) -> DevicesConfig:
    _reject_unknown(raw, {"devices"}, "devices.yaml")
    items = _required(raw, "devices", "devices.yaml")
    if not isinstance(items, list):
        raise ConfigError("devices.yaml 'devices' must be a list")
    devices: dict[str, DeviceInstanceConfig] = {}
    for item in items:
        label = f"device '{item.get('device_id') if isinstance(item, Mapping) else '?'}'"
        item = _expect_mapping(item, "devices.yaml entry")
        _reject_unknown(item, _DEVICE_KEYS, label)
        endpoint = _expect_mapping(_required(item, "endpoint", label), f"{label}.endpoint")
        _reject_unknown(endpoint, _ENDPOINT_KEYS, f"{label}.endpoint")
        port = endpoint.get("port")
        device_id = _as_str(_required(item, "device_id", label), f"{label}.device_id")
        if device_id in devices:
            raise ConfigError(f"Duplicate device_id: '{device_id}'")
        try:
            devices[device_id] = (
                DeviceInstanceConfig(
                    device_id=device_id,
                    model=_as_str(_required(item, "model", label), f"{label}.model"),
                    device_group=_opt_str(item, "device_group", label),
                    endpoint=EndpointConfig(
                        host=_as_str(
                            _required(endpoint, "host", f"{label}.endpoint"),
                            f"{label}.endpoint.host",
                        ),
                        port=(
                            None if port is None else _as_int(port, f"{label}.endpoint.port")
                        ),
                        extensions=_expect_mapping(
                            endpoint.get("extensions") or {}, f"{label}.endpoint.extensions"
                        ),
                    ),
                    enabled=_as_bool(item.get("enabled", True), f"{label}.enabled"),
                )
            )
        except ValueError as exc:
            raise _vo_error(f"Invalid {label}", exc) from exc
    try:
        return DevicesConfig(devices=devices)
    except ValueError as exc:
        raise _vo_error("Invalid devices configuration", exc) from exc


def dump_devices_config(config: DevicesConfig) -> dict[str, Any]:
    return {"devices": [_dump_device(device) for device in config.devices.values()]}


def _dump_device(device: DeviceInstanceConfig) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "host": device.endpoint.host,
        "port": device.endpoint.port,
        "extensions": _plain(device.endpoint.extensions),
    }
    return {
        "device_id": device.device_id,
        "model": device.model,
        "device_group": device.device_group,
        "endpoint": endpoint,
        "enabled": device.enabled,
    }


# ---------------------------------------------------------------------------
# points.yaml
# ---------------------------------------------------------------------------

_POINT_TABLE_KEYS = {"protocol", "extends", "remove_points", "points"}
_POINT_PATCH_KEYS = {
    "point_id",
    "variable_name",
    "point_groups",
    "address",
    "data_type",
    "scale",
    "offset",
    "unit",
    "description",
    "business_point_id",
}


def parse_point_tables_config(raw: Mapping[str, Any]) -> PointTablesConfig:
    """解析 points.yaml 并完成继承展开，返回完整 PointTableConfig 集合。"""
    _reject_unknown(raw, {"point_tables"}, "points.yaml")
    tables_raw = _expect_mapping(raw.get("point_tables") or {}, "points.yaml 'point_tables'")
    drafts: dict[str, PointTableDraft] = {}
    for name, item in tables_raw.items():
        label = f"point table '{name}'"
        item = _expect_mapping(item, label)
        _reject_unknown(item, _POINT_TABLE_KEYS, label)
        protocol = item.get("protocol")
        if protocol is not None:
            protocol = _as_str(protocol, f"{label}.protocol")
            if protocol not in SUPPORTED_PROTOCOLS:
                raise ConfigError(
                    f"Point table: protocol '{protocol}' must be one of "
                    f"{sorted(SUPPORTED_PROTOCOLS)}"
                )
        remove_points = _as_str_list(item.get("remove_points") or [], f"{label}.remove_points")
        if len(remove_points) != len(set(remove_points)):
            raise ConfigError(f"Duplicate remove_points: {remove_points}")
        patches_raw = item.get("points") or []
        if not isinstance(patches_raw, list):
            raise ConfigError(f"{label}.points must be a list")
        patches = [_parse_point_patch(entry, label) for entry in patches_raw]
        seen: set[str] = set()
        for patch in patches:
            if patch.point_id in seen:
                raise ConfigError(f"Duplicate point_id in table: '{patch.point_id}'")
            seen.add(patch.point_id)
        extends = item.get("extends")
        drafts[_as_str(name, "point table name")] = PointTableDraft(
            protocol=protocol,
            extends=None if extends is None else _as_str(extends, f"{label}.extends"),
            remove_points=tuple(remove_points),
            points=tuple(patches),
        )
    try:
        tables = resolve_point_tables(drafts)
        return PointTablesConfig(tables=tables)
    except ValueError as exc:
        raise _vo_error("Invalid points configuration", exc) from exc


def _parse_point_patch(entry: Any, table_label: str) -> PointPatch:
    entry = _expect_mapping(entry, f"{table_label} point")
    _reject_unknown(entry, _POINT_PATCH_KEYS, f"{table_label} point")
    point_id = _as_str(_required(entry, "point_id", table_label), "point.point_id")
    declared = frozenset(set(entry) - {"point_id"})

    groups = entry.get("point_groups") if "point_groups" in entry else None
    parsed_groups = (
        None if groups is None else tuple(_as_str_list(groups, f"point '{point_id}'.point_groups"))
    )
    address = entry.get("address") if "address" in entry else None
    parsed_address = None
    if address is not None:
        # None 值的 address 键按旧语义丢弃；其余键冻结保留。
        parsed_address = {
            key: value
            for key, value in _expect_mapping(address, f"point '{point_id}'.address").items()
            if value is not None
        }
    scale = entry.get("scale") if "scale" in entry else None
    offset = entry.get("offset") if "offset" in entry else None
    return PointPatch(
        point_id=point_id,
        declared=declared,
        variable_name=entry.get("variable_name") if "variable_name" in entry else None,
        point_groups=parsed_groups,
        address=parsed_address,
        data_type=entry.get("data_type") if "data_type" in entry else None,
        scale=None if scale is None else _as_float(scale, f"point '{point_id}'.scale"),
        offset=None if offset is None else _as_float(offset, f"point '{point_id}'.offset"),
        unit=entry.get("unit") if "unit" in entry else None,
        description=entry.get("description") if "description" in entry else None,
        business_point_id=entry.get("business_point_id"),
    )


def dump_point_tables_config(config: PointTablesConfig) -> dict[str, Any]:
    """序列化为完整、展开后的规范化点表定义（不含继承声明）。

    这是明确的保存语义：``save(POINTS, config)`` 输出完整点位，删除
    extends/remove_points 声明，不反向推断继承关系。
    """
    return {
        "point_tables": {
            name: _dump_point_table(table) for name, table in config.tables.items()
        }
    }


def _dump_point_table(table: PointTableConfig) -> dict[str, Any]:
    return {
        "protocol": table.protocol,
        "points": [
            {
                "point_id": point.point_id,
                "variable_name": point.variable_name,
                "point_groups": list(point.point_groups),
                "address": _plain(point.address),
                "data_type": point.data_type,
                "scale": point.scale,
                "offset": point.offset,
                "unit": point.unit,
                "description": point.description,
                "business_point_id": point.business_point_id,
            }
            for point in table.points.values()
        ],
    }


# ---------------------------------------------------------------------------
# tasks.yaml
# ---------------------------------------------------------------------------

_TASK_KEYS = {"task_id", "device", "device_group", "point_group", "interval", "targets", "enabled"}
_TARGET_KEYS = {"sink"}


def parse_tasks_config(raw: Mapping[str, Any]) -> TasksConfig:
    _reject_unknown(raw, {"tasks"}, "tasks.yaml")
    items = raw.get("tasks") or []
    if not isinstance(items, list):
        raise ConfigError("tasks.yaml 'tasks' must be a list")
    tasks: dict[str, TaskConfig] = {}
    for item in items:
        label = f"task '{item.get('task_id') if isinstance(item, Mapping) else '?'}'"
        item = _expect_mapping(item, "tasks.yaml entry")
        _reject_unknown(item, _TASK_KEYS, label)
        task_id = _as_str(_required(item, "task_id", label), f"{label}.task_id")
        targets_raw = _required(item, "targets", label)
        if not isinstance(targets_raw, list):
            raise ConfigError(f"{label}.targets must be a list")
        targets: list[str] = []
        for target in targets_raw:
            target = _expect_mapping(target, f"{label}.targets entry")
            _reject_unknown(target, _TARGET_KEYS, f"{label}.targets entry")
            targets.append(
                _as_str(_required(target, "sink", f"{label}.targets"), f"{label}.targets.sink")
            )
        if task_id in tasks:
            raise ConfigError(f"Duplicate task_id: '{task_id}'")
        interval = item.get("interval")
        try:
            tasks[task_id] = (
                TaskConfig(
                    task_id=task_id,
                    device=_opt_str(item, "device", label),
                    device_group=_opt_str(item, "device_group", label),
                    point_group=_as_str(
                        _required(item, "point_group", label), f"{label}.point_group"
                    ),
                    interval=(
                        None if interval is None else _as_float(interval, f"{label}.interval")
                    ),
                    targets=tuple(targets),
                    enabled=_as_bool(item.get("enabled", True), f"{label}.enabled"),
                )
            )
        except ValueError as exc:
            raise _vo_error(f"Invalid {label}", exc) from exc
    try:
        return TasksConfig(tasks=tasks)
    except ValueError as exc:
        raise _vo_error("Invalid tasks configuration", exc) from exc


def dump_tasks_config(config: TasksConfig) -> dict[str, Any]:
    return {
        "tasks": [
            {
                "task_id": task.task_id,
                "device": task.device,
                "device_group": task.device_group,
                "point_group": task.point_group,
                "interval": task.interval,
                "targets": [{"sink": sink} for sink in task.targets],
                "enabled": task.enabled,
            }
            for task in config.tasks.values()
        ]
    }


# ---------------------------------------------------------------------------
# units.yaml
# ---------------------------------------------------------------------------

_UNIT_KEYS = {"symbol", "name"}


def parse_units_config(raw: Mapping[str, Any]) -> UnitsConfig:
    _reject_unknown(raw, {"units"}, "units.yaml")
    items = _expect_mapping(raw.get("units") or {}, "units.yaml 'units'")
    units: dict[str, UnitDefinitionConfig] = {}
    for unit_id, item in items.items():
        label = f"unit '{unit_id}'"
        item = _expect_mapping(item, label)
        _reject_unknown(item, _UNIT_KEYS, label)
        symbol = item.get("symbol", "")
        if not isinstance(symbol, str):
            raise ConfigError(f"{label}.symbol must be a string")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"{label}.name must be a string")
        units[_as_str(unit_id, "unit id")] = UnitDefinitionConfig(symbol=symbol, name=name)
    return UnitsConfig(units=units)


def dump_units_config(config: UnitsConfig) -> dict[str, Any]:
    return {
        "units": {
            unit_id: {"symbol": unit.symbol, "name": unit.name}
            for unit_id, unit in config.units.items()
        }
    }


# ---------------------------------------------------------------------------
# sinks.yaml（沿用既有强类型 Sink 契约模型）
# ---------------------------------------------------------------------------


def parse_sinks_config(raw: Mapping[str, Any]) -> SinksConfig:
    try:
        return SinksConfig.model_validate(raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid sinks configuration: {exc}") from exc


def dump_sinks_config(config: SinksConfig) -> dict[str, Any]:
    return config.model_dump(mode="python", by_alias=True)


__all__ = [
    "ADS_READ_MODES",
    "SUPPORTED_PROTOCOLS",
    "dump_device_models_config",
    "dump_devices_config",
    "dump_point_tables_config",
    "dump_sinks_config",
    "dump_system_config",
    "dump_tasks_config",
    "dump_units_config",
    "parse_device_models_config",
    "parse_devices_config",
    "parse_point_tables_config",
    "parse_sinks_config",
    "parse_system_config",
    "parse_tasks_config",
    "parse_units_config",
    "validate_ams_net_id",
]


def validate_ams_net_id(net_id: str) -> str:
    """校验六段十进制 AMS Net ID，非法时抛 ConfigError。"""
    if not is_valid_ams_net_id(net_id):
        raise ConfigError(
            f"Invalid AMS Net ID '{net_id}'; expected dotted-numeric 'a.b.c.d.e.f'"
        )
    return net_id
