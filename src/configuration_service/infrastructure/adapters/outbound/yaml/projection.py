"""Typed relational projections of validated configuration YAML objects.

The canonical JSON root preserves every optional field and scalar type;
specific relation rows provide queryable, foreign-key-protected entities.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from configuration_service.domain.models import PreparedDocument, RowProjection
from configuration_service.infrastructure.adapters.outbound.yaml.codec import canonical_json


def _objects(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object mapping")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a nonempty string")
    return value


def create_projections(
    document: PreparedDocument,
    *,
    dependency_versions: Mapping[str, str],
    device_bindings: Mapping[str, tuple[str, str, str, str]],
    table_device_type: str | None,
) -> tuple[RowProjection, ...]:
    """Project normalized entities, binding cross-document keys to exact versions."""
    raw = document.canonical_payload
    rows: list[RowProjection] = [
        RowProjection(
            table="config_entries",
            columns={
                "object_type": "document",
                "object_id": "root",
                "properties_json": canonical_json(raw),
            },
        )
    ]
    config_type = document.identity.config_type

    if config_type == "units":
        quantities = _objects(raw.get("quantities", {}), "quantities")
        for unit_id, entry in sorted(_objects(raw["units"], "units").items()):
            unit = _objects(entry, f"units.{unit_id}")
            quantity_id = _text(unit.get("quantity"), "quantity")
            quantity = _objects(quantities.get(quantity_id, {}), "quantity definition")
            rows.append(
                RowProjection(
                    "unit_definitions",
                    {
                        "unit_id": _text(unit_id, "unit_id"),
                        "quantity_id": quantity_id,
                        "symbol": str(unit.get("symbol", unit_id)),
                        "scale_to_base": str(unit.get("scale_to_base", 1)),
                        "offset_to_base": str(unit.get("offset_to_base", 0)),
                        "base_unit_id": quantity.get("base_unit"),
                    },
                )
            )

    elif config_type == "business_points":
        units_version = _text(
            dependency_versions.get("units"), "business standard Units dependency"
        )
        for business_id, entry in sorted(
            _objects(raw["business_points"], "business_points").items()
        ):
            point = _objects(entry, f"business_points.{business_id}")
            rows.append(
                RowProjection(
                    "business_points",
                    {
                        "business_point_id": _text(business_id, "business_point_id"),
                        "data_type": _text(point.get("data_type"), "data_type"),
                        "standard_unit_version_id": units_version,
                        "standard_unit_id": _text(point.get("unit"), "standard unit"),
                        "description": point.get("description"),
                    },
                )
            )

    elif config_type == "service":
        service_id = raw.get("service_id", document.identity.config_id)
        rows.append(
            RowProjection(
                "app_service_configs",
                {
                    "service_id": _text(service_id, "service_id"),
                    "module_type": str(raw.get("module_type", document.identity.config_id)),
                    "params_json": canonical_json(raw),
                },
            )
        )

    elif config_type == "device":
        for device_id, entry in sorted(_objects(raw["devices"], "devices").items()):
            device = _objects(entry, f"devices.{device_id}")
            binding = device_bindings.get(device_id)
            if binding is None:
                # Legacy imports without pinned templates retain complete JSON;
                # never manufacture version IDs to satisfy a relational foreign key.
                continue
            model_version_id, model_id, table_version_id, table_id = binding
            rows.append(
                RowProjection(
                    "devices",
                    {
                        "device_id": _text(device_id, "device_id"),
                        "model_version_id": model_version_id,
                        "model_id": model_id,
                        "point_table_version_id": table_version_id,
                        "point_table_id": table_id,
                        "properties_json": canonical_json(device),
                    },
                )
            )

    elif config_type == "device_model":
        if "device_models" in raw:
            models = _objects(raw["device_models"], "device_models")
        else:
            models = {str(raw["template_id"]): raw}
        for model_id, entry in sorted(models.items()):
            model = _objects(entry, f"device_models.{model_id}")
            device_type = model.get("device_type", model.get("device_type_id"))
            rows.append(
                RowProjection(
                    "device_models",
                    {
                        "model_id": _text(model_id, "model_id"),
                        "device_type": _text(device_type, "device_type"),
                        "properties_json": canonical_json(model),
                    },
                )
            )

    elif config_type == "point_table":
        point_table_device_type = raw.get("device_type", table_device_type)
        immutable_id = f"point_table_{_text(raw['template_id'], 'template_id')}_v{document.file_version}"
        if point_table_device_type is not None:
            rows.append(
                RowProjection(
                    "point_tables",
                    {
                        "point_table_id": immutable_id,
                        "device_type": _text(point_table_device_type, "device_type"),
                        "protocol": _text(raw["protocol"], "protocol"),
                        "parent_version_id": raw.get("parent_version_id"),
                        "parent_point_table_id": raw.get("parent_point_table_id"),
                    },
                )
            )
            if "business_points" in dependency_versions and "units" in dependency_versions:
                business_version = _text(
                    dependency_versions.get("business_points"), "business_points dependency"
                )
                units_version = _text(dependency_versions.get("units"), "units dependency")
                for point_id, entry in sorted(_objects(raw["points"], "points").items()):
                    point = _objects(entry, f"points.{point_id}")
                    rows.append(
                        RowProjection(
                            "point_mappings",
                            {
                                "point_table_id": immutable_id,
                                "point_id": _text(point_id, "point_id"),
                                "business_config_version_id": business_version,
                                "business_point_id": _text(
                                    point.get("business_point_id"), "business_point_id"
                                ),
                                "source_type": _text(point.get("data_type"), "data_type"),
                                "source_unit_version_id": units_version,
                                "source_unit_id": _text(point.get("unit"), "unit"),
                                "scale": str(point.get("scale", 1)),
                                "offset": str(point.get("offset", 0)),
                                "address_json": canonical_json(point.get("address", {})),
                                "point_groups_json": canonical_json(point.get("point_groups", [])),
                            },
                        )
                    )

    # Structured objects share the fixed version key and are independently
    # queryable, while the canonical root remains the exact YAML reconstruction.
    for group, value in sorted(raw.items()):
        if group in {"schema_version", "file_version", "config_sources"}:
            continue
        if isinstance(value, dict):
            for object_id, entry in sorted(value.items()):
                if isinstance(entry, dict):
                    rows.append(
                        RowProjection(
                            "config_entries",
                            {
                                "object_type": group,
                                "object_id": str(object_id),
                                "properties_json": canonical_json(entry),
                            },
                        )
                    )
        elif isinstance(value, list):
            for index, entry in enumerate(value):
                if isinstance(entry, dict):
                    rows.append(
                        RowProjection(
                            "config_entries",
                            {
                                "object_type": group,
                                "object_id": str(index),
                                "properties_json": canonical_json(entry),
                            },
                        )
                    )

    # Device and point-table references require exact published version IDs.
    # Unresolved legacy template aliases remain in the lossless root, and their
    # binding must be rejected by the application before a typed revision is published.
    return tuple(rows)
