"""Build a shared typed domain snapshot without service-specific settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.errors import ConfigError
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
    PointTable,
    PointTableId,
    Site,
    validate_core_config,
)
from core.domain.unit import UnitCatalog

from .business_points import parse_business_points
from .shared_documents import SharedDocuments
from .shared_point_tables import _point_tables
from .units import parse_unit_catalog


@dataclass(frozen=True, slots=True)
class SharedDomainSnapshot:
    """Resolved Core entities with no tasks, sinks, or process settings."""

    site: Site
    device_types: Mapping[DeviceTypeId, DeviceType]
    device_models: Mapping[DeviceModelId, DeviceModel]
    device_groups: Mapping[DeviceGroupId, DeviceGroup]
    point_tables: Mapping[PointTableId, PointTable]
    units: UnitCatalog
    business_points: Mapping[BusinessPointId, BusinessPoint]
    revision: str


def _content(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"schema_version", "file_version", "template_id"}
    }


def build_shared_snapshot(documents: SharedDocuments) -> SharedDomainSnapshot:
    """Resolve entities and validate references; never access the network."""
    units = parse_unit_catalog(_content(documents.units))
    business_points = parse_business_points(_content(documents.business_points), units)
    definitions: dict[str, dict[str, Any]] = {}
    device_types: dict[DeviceTypeId, DeviceType] = {}
    models: dict[DeviceModelId, DeviceModel] = {}
    groups: dict[DeviceGroupId, DeviceGroup] = {}
    devices: dict[DeviceId, Device] = {}

    for device_id, definition in documents.devices.items():
        model = definition.templates.model
        model_data = _content(model.document)
        protocol = definition.templates.protocol.document.get("protocol")
        if protocol not in {"ads", "modbus", "iec104"}:
            raise ConfigError(f"{device_id}: unsupported protocol")
        table = definition.templates.point_table
        table_id = f"{table.reference.template_id}@{table.reference.file_version}"
        parents = tuple(reversed(definition.templates.inherited_tables))
        for index, parent in enumerate(parents):
            parent_data = _content(parent.document)
            parent_id = f"{parent.reference.template_id}@{parent.reference.file_version}"
            entry: dict[str, Any] = {
                "protocol": protocol,
                "points": parent_data.get("points", {}),
                "remove_points": parent_data.get("remove_points", []),
            }
            if index:
                earlier = parents[index - 1].reference
                entry["parent_id"] = f"{earlier.template_id}@{earlier.file_version}"
            definitions[parent_id] = entry

        table_data = _content(table.document)
        entry = {
            "protocol": protocol,
            "points": table_data.get("points", {}),
            "remove_points": table_data.get("remove_points", []),
        }
        if definition.templates.inherited_tables:
            immediate = definition.templates.inherited_tables[0].reference
            entry["parent_id"] = f"{immediate.template_id}@{immediate.file_version}"
        definitions[table_id] = entry

        type_id = DeviceTypeId(str(model_data.get("device_type_id", "generic")))
        device_types.setdefault(type_id, DeviceType(type_id, str(type_id)))
        model_id = DeviceModelId(f"{model.reference.template_id}@{model.reference.file_version}")
        models[model_id] = DeviceModel(
            device_model_id=model_id,
            device_type_id=type_id,
            point_table_id=PointTableId(table_id),
            name=model_data.get("name"),
            manufacturer=model_data.get("manufacturer"),
            endpoint_defaults=model_data.get("endpoint_defaults", {}),
            protocol_options=model_data.get("protocol_options", {}),
        )
        group_id = DeviceGroupId(device_id)
        groups[group_id] = DeviceGroup(group_id, device_id)
        options = dict(definition.protocol_options)
        subscribe = options.pop("subscribe_enabled", False)
        if type(subscribe) is not bool or (subscribe and protocol != "ads"):
            raise ConfigError(f"{device_id}: invalid subscribe_enabled")
        endpoint = definition.endpoint
        devices[DeviceId(device_id)] = Device(
            device_id=DeviceId(device_id),
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
        )
        site = Site(
            site_id=documents.site["site_id"],
            name=documents.site.get("name", documents.site["site_id"]),
            devices=devices,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid shared domain configuration: {exc}") from exc

    return SharedDomainSnapshot(
        site=site,
        device_types=device_types,
        device_models=models,
        device_groups=groups,
        point_tables=point_tables,
        units=units,
        business_points=business_points,
        revision=documents.revision,
    )
