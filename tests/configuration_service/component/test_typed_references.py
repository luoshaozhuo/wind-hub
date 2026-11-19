"""Exact-version domain projections and composite foreign keys.

The v2 schema retired the intermediate semantic_points catalog: global
BusinessPoint rows bind the Units standard directly, and PointTable mappings
bind the immutable BusinessPoint and Units versions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import RepositoryNotFoundError, RowProjection
from tests.configuration_service.conftest import document_input, publish_documents

pytestmark = pytest.mark.component


def ref(config_type: str, config_id: str, version: str) -> dict[str, str]:
    return {"config_type": config_type, "config_id": config_id, "file_version": version}


def _units(version: str = "1.0.0"):
    return document_input(
        "units",
        "standard",
        version,
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )


def _business(version: str = "1.0.0", *, units_version: str = "1.0.0"):
    return document_input(
        "business_points",
        "standard",
        version,
        business_points={
            "active_power": {"data_type": "float", "unit": "watt"},
        },
        config_sources=[ref("units", "standard", units_version)],
    )


def _point_table(version: str = "1.0.0", *, table_id: str = "wtg-modbus"):
    return document_input(
        "point_table",
        table_id,
        version,
        template_id=table_id,
        device_type="turbine",
        protocol="modbus",
        points={
            "power": {
                "business_point_id": "active_power",
                "data_type": "float",
                "unit": "watt",
                "address": {"kind": "holding", "address": 1},
            }
        },
        config_sources=[
            ref("business_points", "standard", "1.0.0"),
            ref("units", "standard", "1.0.0"),
        ],
    )


def test_units_business_point_table_projection_chain(
    app: ConfigurationApplication,
) -> None:
    units = _units()
    business = _business()
    point_table = _point_table()
    revision, _ = publish_documents(
        app,
        (units, business, point_table),
        request_id="typed-chain",
    )
    unit_version = app.repository.resolve_version(units.identity, "1.0.0")
    business_version = app.repository.resolve_version(business.identity, "1.0.0")
    point_version = app.repository.resolve_version(point_table.identity, "1.0.0")
    assert (
        app.repository.get_projection_rows(unit_version, "unit_definitions")[0]["unit_id"] == "watt"
    )
    business_row = app.repository.get_projection_rows(business_version, "business_points")[0]
    assert business_row["business_point_id"] == "active_power"
    assert business_row["standard_unit_version_id"] == unit_version
    point_row = app.repository.get_projection_rows(point_version, "point_mappings")[0]
    assert point_row["business_config_version_id"] == business_version
    assert point_row["source_unit_version_id"] == unit_version
    bundle = app.repository.get_bundle_versions((point_table.identity,), revision, max_versions=10)
    assert [row["version_id"] for row in bundle] == [
        unit_version,
        business_version,
        point_version,
    ]


def test_duplicate_pinned_unit_binding_is_rejected(
    app: ConfigurationApplication,
) -> None:
    # Both pins resolve to the one units version present, so the duplicate
    # binding guard fires before any version lookup can fail.
    double_pinned = document_input(
        "business_points",
        "standard",
        "1.0.0",
        business_points={
            "active_power": {"data_type": "float", "unit": "watt"},
        },
        config_sources=[
            ref("units", "standard", "1.0.0"),
            ref("units", "standard", "1.0.0"),
        ],
    )
    with pytest.raises(ValueError, match="duplicate units standard binding"):
        app.create_candidate(
            actor_id="publisher",
            request_id="ambiguous-consumer",
            expected_revision=0,
            documents=(_units("1.0.0"), double_pinned),
            validate_only=True,
        )


def test_point_table_without_points_keeps_json_without_manufacturing_foreign_keys(
    app: ConfigurationApplication,
) -> None:
    # A PointTable without points still closes over the standards but
    # manufactures no point mapping foreign keys.
    unbound = document_input(
        "point_table",
        "unbound-table",
        "1.0.0",
        template_id="unbound-table",
        device_type="turbine",
        protocol="modbus",
        points={},
        config_sources=[
            ref("business_points", "standard", "1.0.0"),
            ref("units", "standard", "1.0.0"),
        ],
    )
    _, version_ids = publish_documents(
        app, (_units(), _business(), unbound), request_id="unbound-points"
    )
    version_id = app.repository.resolve_version(unbound.identity, "1.0.0")
    assert version_id in version_ids
    assert app.repository.get_projection_rows(version_id, "point_mappings") == ()
    assert app.repository.get_projection_rows(version_id, "config_entries")


def test_unbound_device_projection_keeps_json_without_manufacturing_foreign_keys() -> None:
    # Full-directory validation rejects devices without a model at staging, so
    # the legacy no-binding guarantee is pinned at the projection boundary:
    # an unresolved binding never fabricates a typed foreign-key row.
    from configuration_service.domain.models import ConfigIdentity, PreparedDocument
    from configuration_service.infrastructure.adapters.outbound.yaml.projection import (
        create_projections,
    )

    document = PreparedDocument(
        identity=ConfigIdentity("device", "devices"),
        schema_ref="device:1",
        file_version="1.0.0",
        content_sha256="0" * 64,
        canonical_payload={
            "schema_version": 1,
            "file_version": "1.0.0",
            "devices": {"ORPHAN-01": {"name": "Orphan"}},
        },
    )
    rows = create_projections(
        document,
        dependency_versions={},
        device_bindings={},
        table_device_type=None,
    )
    assert [row.table for row in rows].count("devices") == 0
    assert any(
        row.table == "config_entries" and row.columns["object_type"] == "devices" for row in rows
    )


def test_full_device_point_dependency_chain_creates_composite_foreign_key_rows(
    app: ConfigurationApplication,
) -> None:
    units = _units()
    business = _business()
    point_table = _point_table(table_id="turbine-modbus")
    model = document_input(
        "device_model",
        "turbine-model",
        "1.0.0",
        template_id="turbine-model",
        name="Turbine model",
        device_type_id="turbine",
        point_table={"id": "turbine-modbus", "version": "1.0.0"},
    )
    device = document_input(
        "device",
        "devices",
        "1.0.0",
        devices={
            "TURBINE-01": {
                "name": "Turbine 1",
                "model": {"id": "turbine-model", "version": "1.0.0"},
                "endpoint": {"host": "127.0.0.1", "port": 502},
            },
            "TURBINE-02": {
                "name": "Turbine 2",
                "model": {"id": "turbine-model", "version": "1.0.0"},
                "endpoint": {"host": "127.0.0.1", "port": 503},
            },
        },
    )
    revision, version_ids = publish_documents(
        app,
        (units, business, point_table, model, device),
        request_id="full-device-chain",
    )
    unit_version = app.repository.resolve_version(units.identity, "1.0.0")
    business_version = app.repository.resolve_version(business.identity, "1.0.0")
    table_version = app.repository.resolve_version(point_table.identity, "1.0.0")
    model_version = app.repository.resolve_version(model.identity, "1.0.0")
    device_version = app.repository.resolve_version(device.identity, "1.0.0")
    assert len(app.repository.get_projection_rows(unit_version, "unit_definitions")) == 1
    business_rows = app.repository.get_projection_rows(business_version, "business_points")
    assert {row["standard_unit_version_id"] for row in business_rows} == {unit_version}
    table_rows = app.repository.get_projection_rows(table_version, "point_tables")
    assert len(table_rows) == 1
    assert table_rows[0]["point_table_id"] == "point_table_turbine-modbus_v1.0.0"
    mapping_rows = app.repository.get_projection_rows(table_version, "point_mappings")
    assert len(mapping_rows) == 1
    assert {row["business_config_version_id"] for row in mapping_rows} == {business_version}
    assert {row["source_unit_version_id"] for row in mapping_rows} == {unit_version}
    model_rows = app.repository.get_projection_rows(model_version, "device_models")
    assert len(model_rows) == 1
    assert model_rows[0]["model_id"] == "turbine-model"
    device_rows = app.repository.get_projection_rows(device_version, "devices")
    assert len(device_rows) == 2
    assert {row["model_version_id"] for row in device_rows} == {model_version}
    assert {row["point_table_version_id"] for row in device_rows} == {table_version}
    assert {row["device_id"] for row in device_rows} == {"TURBINE-01", "TURBINE-02"}
    with app.repository.read() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    historical = app.repository.get_bundle_versions((device.identity,), revision, max_versions=10)
    assert {row["version_id"] for row in historical} == set(version_ids)


def test_missing_dependency_cannot_publish_and_whole_chain_rolls_back(
    app: ConfigurationApplication,
) -> None:
    units = _units()
    broken = document_input(
        "business_points",
        "standard",
        "1.0.0",
        business_points={"active_power": {"data_type": "float", "unit": "watt"}},
        config_sources=[ref("units", "standard", "9.9.9")],
    )
    with pytest.raises(RepositoryNotFoundError, match="referenced configuration version not found"):
        app.create_candidate(
            actor_id="publisher",
            request_id="broken-dependency",
            expected_revision=0,
            documents=(units, broken),
            validate_only=True,
        )
    assert app.repository.current_revision() == 0


def model_table_device(version: str):
    table = document_input(
        "point_table",
        "upgrade-table",
        version,
        template_id="upgrade-table",
        device_type="turbine",
        protocol="modbus",
        points={},
        config_sources=[
            ref("business_points", "standard", "1.0.0"),
            ref("units", "standard", "1.0.0"),
        ],
    )
    model = document_input(
        "device_model",
        "upgrade-model",
        version,
        template_id="upgrade-model",
        device_type_id="turbine",
        point_table={"id": "upgrade-table", "version": version},
    )
    device = document_input(
        "device",
        "devices",
        version,
        devices={"TURBINE-01": {"model": {"id": "upgrade-model", "version": version}}},
    )
    return table, model, device


def test_model_upgrade_preserves_historical_device_bindings(
    app: ConfigurationApplication,
) -> None:
    standards = (_units(), _business())
    table1, model1, device1 = model_table_device("1.0.0")
    revision1, ids1 = publish_documents(
        app,
        (*standards, table1, model1, device1),
        request_id="model-v1",
    )
    table_v1 = app.repository.resolve_version(table1.identity, "1.0.0")
    model_v1 = app.repository.resolve_version(model1.identity, "1.0.0")
    device_v1 = app.repository.resolve_version(device1.identity, "1.0.0")
    table2, model2, device2 = model_table_device("2.0.0")
    revision2, ids2 = publish_documents(
        app,
        (*standards, table2, model2, device2),
        expected_revision=revision1,
        request_id="model-v2",
    )
    table_v2 = app.repository.resolve_version(table2.identity, "2.0.0")
    model_v2 = app.repository.resolve_version(model2.identity, "2.0.0")
    device_v2 = app.repository.resolve_version(device2.identity, "2.0.0")
    old_device = app.repository.get_projection_rows(device_v1, "devices")[0]
    new_device = app.repository.get_projection_rows(device_v2, "devices")[0]
    assert old_device["model_version_id"] == model_v1
    assert new_device["model_version_id"] == model_v2
    assert old_device["point_table_version_id"] == table_v1
    assert new_device["point_table_version_id"] == table_v2
    device_identity = model_table_device("1.0.0")[2].identity
    assert app.get_config(device_identity, revision_id=revision1).version_id == device_v1
    assert app.get_config(device_identity, revision_id=revision2).version_id == device_v2


def test_foreign_key_failure_inside_publish_rolls_back_every_document(
    app: ConfigurationApplication,
) -> None:
    device_doc = model_table_device("1.0.0")[2]
    documents = (_units(), _business(), *model_table_device("1.0.0"))
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="fk-rollback-create",
        expected_revision=0,
        documents=documents,
        validate_only=False,
    )
    versions = list(app._compile_versions(prepared, 0))
    device_index = next(
        index
        for index, version in enumerate(versions)
        if version.document.identity == device_doc.identity
    )
    device_version = versions[device_index]
    broken_projections = tuple(
        RowProjection(
            row.table,
            {**row.columns, "model_version_id": "missing-model-version"}
            if row.table == "devices"
            else row.columns,
        )
        for row in device_version.projections
    )
    versions[device_index] = replace(device_version, projections=broken_projections)
    with pytest.raises(sqlite3.IntegrityError):
        app.repository.publish_candidate(
            actor_id="publisher",
            request_id="fk-rollback-publish",
            candidate_id=candidate,
            expected_revision=0,
            versions=versions,
        )
    assert app.repository.current_revision() == 0
    with app.repository.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
