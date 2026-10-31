"""Core domain snapshot from Collector configuration documents."""

from __future__ import annotations

from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.settings import (
    ADSLocalConfig,
    LogSettings,
    MessagingSettings,
    RuntimeSettings,
    SystemSettings,
    ThreadPoolSettings,
)
from core.domain import (
    ConnectionEndpoint,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    PointTableId,
    Site,
    Task,
    validate_core_config,
)

from .business_points import parse_business_points
from .collector_documents import CollectorDocuments
from .parse_domain import _OPTION_PARSERS, _point_tables
from .units import parse_unit_catalog


def _content(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in raw.items()
        if k not in {"schema_version", "template_id", "template_version"}
    }


def _settings(raw: dict[str, Any]) -> SystemSettings:
    runtime = dict(raw.get("runtime", {}))
    watchdog = raw.get("watchdog", {})
    runtime["watchdog_interval"] = watchdog.get("interval", 5.0)
    runtime["health_timeout"] = watchdog.get("health_timeout", 1.0)
    if "connect_timeout" in watchdog:
        runtime["connect_timeout"] = watchdog["connect_timeout"]
    if "thread_pool" in runtime:
        runtime["thread_pool"] = ThreadPoolSettings(**runtime["thread_pool"])
    messaging = raw["messaging"]
    try:
        return SystemSettings(
            runtime=RuntimeSettings(**runtime),
            messaging=MessagingSettings(
                servers=tuple(messaging["servers"]),
                delivery=messaging["delivery"],
                max_staleness_ms=messaging.get("max_staleness_ms"),
            ),
            ads=ADSLocalConfig(**raw["ads"]) if "ads" in raw else None,
            logs=LogSettings(**raw.get("logs", {})),
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"invalid Collector service settings: {exc}") from exc


def build_collector_snapshot(documents: CollectorDocuments) -> ConfigSnapshot:
    """Resolve typed domain objects; never perform network or file writes."""
    units = parse_unit_catalog(_content(dict(documents.units)))
    business_points = parse_business_points(_content(dict(documents.business_points)), units)
    definitions: dict[str, dict[str, Any]] = {}
    device_types: dict[DeviceTypeId, DeviceType] = {}
    models: dict[DeviceModelId, DeviceModel] = {}
    groups: dict[DeviceGroupId, DeviceGroup] = {}
    devices: dict[DeviceId, Device] = {}
    tasks: dict[str, Task] = {}
    for identity, definition in documents.devices.items():
        model_id = DeviceModelId(
            f"{definition.model.reference.template_id}@"
            f"{definition.model.reference.template_version}"
        )
        model_data = _content(dict(definition.model.document))
        protocol_data = _content(dict(definition.templates["protocol"].document))
        protocol = protocol_data.get("protocol")
        if protocol not in ("ads", "modbus", "iec104"):
            raise ConfigError(f"{identity}: unsupported protocol")
        table = definition.templates["point_table"]
        table_id = f"{table.reference.template_id}@{table.reference.template_version}"
        table_data = _content(dict(table.document))
        if table_data.get("protocol") != protocol:
            raise ConfigError(f"{identity}: protocol/point table mismatch")
        parents = tuple(reversed(definition.inherited_tables))
        for index, parent in enumerate(parents):
            parent_raw = _content(dict(parent.document))
            entry = {
                "protocol": protocol,
                "points": parent_raw.get("points", {}),
                "remove_points": parent_raw.get("remove_points", []),
            }
            if index:
                entry["parent_id"] = (
                    f"{parents[index - 1].reference.template_id}@"
                    f"{parents[index - 1].reference.template_version}"
                )
            definitions[f"{parent.reference.template_id}@{parent.reference.template_version}"] = (
                entry
            )
        definitions[table_id] = {
            "protocol": protocol,
            "points": table_data.get("points", {}),
            "remove_points": table_data.get("remove_points", []),
            **(
                {
                    "parent_id": (
                        f"{definition.inherited_tables[0].reference.template_id}@"
                        f"{definition.inherited_tables[0].reference.template_version}"
                    )
                }
                if definition.inherited_tables
                else {}
            ),
        }
        type_id = DeviceTypeId(str(model_data.get("device_type_id", "generic")))
        device_types.setdefault(type_id, DeviceType(type_id, str(type_id)))
        models[model_id] = DeviceModel(
            device_model_id=model_id,
            device_type_id=type_id,
            point_table_id=PointTableId(table_id),
            name=model_data.get("name"),
            manufacturer=model_data.get("manufacturer"),
            endpoint_defaults=model_data.get("endpoint_defaults", {}),
            protocol_options=model_data.get("protocol_options", {}),
        )
        group_id = DeviceGroupId(identity)
        groups[group_id] = DeviceGroup(group_id, identity)
        endpoint = definition.endpoint
        options = dict(definition.overrides.get("protocol_options", {}))
        subscribe = options.pop("subscribe_enabled", False)
        if subscribe and protocol != "ads":
            raise ConfigError("subscribe_enabled requires ADS protocol")
        if not isinstance(subscribe, bool):
            raise ConfigError("subscribe_enabled must be boolean")
        effective_options = {**model_data.get("protocol_options", {}), **options}
        parsed_endpoint = ConnectionEndpoint(
            host=endpoint["host"],
            port=endpoint.get("port", model_data.get("endpoint_defaults", {}).get("port")),
        )
        try:
            _OPTION_PARSERS[protocol](parsed_endpoint, effective_options)
        except (TypeError, ValueError, KeyError) as exc:
            raise ConfigError(f"{identity}: invalid protocol options: {exc}") from exc
        devices[DeviceId(identity)] = Device(
            device_id=DeviceId(identity),
            device_model_id=model_id,
            name=definition.name,
            endpoint=ConnectionEndpoint(
                host=endpoint["host"],
                port=endpoint.get("port"),
            ),
            protocol_options=options,
            subscribe_enabled=subscribe,
            device_group_ids=(group_id,),
            enabled=definition.enabled,
        )
        task_data = _content(dict(definition.templates["task"].document))
        tasks[identity] = Task(
            task_id=identity,
            device_group_id=group_id,
            point_group=task_data.get("point_group", "dynamic"),
            interval=task_data.get("interval", 1.0),
            enabled=definition.enabled,
        )
    point_tables = _point_tables({"point_tables": definitions}, business_points, units)
    try:
        validate_core_config(
            units=units,
            device_types=device_types,
            device_models=models,
            device_groups=groups,
            devices=devices,
            business_points=business_points,
            point_tables=point_tables,
            tasks=tasks,
        )
        site_id = documents.site["site_id"]
        site = Site(
            site_id=site_id,
            name=documents.site.get("name", site_id),
            devices=devices,
        )
        return ConfigSnapshot(
            system=_settings(dict(documents.service)),
            units=units,
            site=site,
            device_types=device_types,
            device_models=models,
            device_groups=groups,
            point_tables=point_tables,
            point_table_baseline=point_tables,
            point_table_definitions=definitions,
            business_points=business_points,
            tasks=tasks,
            sinks={},
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"invalid Collector domain snapshot: {exc}") from exc
