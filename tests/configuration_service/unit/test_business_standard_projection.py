"""Global BusinessPoint and versioned PointTable projection invariants."""

from __future__ import annotations

from configuration_service.domain.models import ConfigIdentity, PreparedDocument
from configuration_service.infrastructure.adapters.outbound.yaml.projection import (
    create_projections,
)


def _document(config_type: str, config_id: str, body: dict[str, object]) -> PreparedDocument:
    return PreparedDocument(
        identity=ConfigIdentity(config_type, config_id),
        schema_ref=f"{config_type}:1",
        file_version="1.0.0",
        content_sha256="0" * 64,
        canonical_payload=body,
    )


def test_business_identity_is_global_not_device_scoped() -> None:
    result = create_projections(
        _document(
            "business_points",
            "standard",
            {
                "business_points": {
                    "active_power": {
                        "data_type": "float32",
                        "unit": "kilowatt",
                        "description": "Active power",
                    }
                }
            },
        ),
        dependency_versions={"units": "fixed-units-version"},
        device_bindings={},
        table_device_type=None,
    )
    item = next(row for row in result if row.table == "business_points")
    assert item.columns["business_point_id"] == "active_power"
    assert item.columns["standard_unit_version_id"] == "fixed-units-version"
    assert item.columns["standard_unit_id"] == "kilowatt"
    assert "device_type" not in item.columns
    assert "semantic_config_version_id" not in item.columns


def test_point_table_mapping_uses_immutable_fully_qualified_identity() -> None:
    result = create_projections(
        _document(
            "point_table",
            "modbus_wtg_sim",
            {
                "template_id": "modbus_wtg_sim",
                "protocol": "modbus",
                "points": {
                    "power": {
                        "business_point_id": "active_power",
                        "data_type": "float32",
                        "unit": "kilowatt",
                        "address": {"register_type": "input", "address": 0},
                    }
                },
            },
        ),
        dependency_versions={
            "business_points": "fixed-business-version",
            "units": "fixed-units-version",
        },
        device_bindings={},
        table_device_type="turbine",
    )
    tables = [row for row in result if row.table == "point_tables"]
    mappings = [row for row in result if row.table == "point_mappings"]
    immutable = "point_table_modbus_wtg_sim_v1.0.0"
    assert len(tables) == len(mappings) == 1
    assert tables[0].columns["point_table_id"] == immutable
    assert mappings[0].columns["point_table_id"] == immutable
    assert mappings[0].columns["business_config_version_id"] == "fixed-business-version"
    assert "device_type" not in mappings[0].columns
