"""Exact-version domain projections and composite foreign keys."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import RepositoryNotFoundError, RowProjection
from tests.configuration_service.component.test_shipped_yaml_repository import input_for
from tests.configuration_service.conftest import document_input, publish_documents
from tests.configuration_service.unit.test_yaml_acceptance import ROOT

pytestmark = pytest.mark.component


def ref(config_type: str, config_id: str, version: str) -> dict[str, str]:
    return {"config_type": config_type, "config_id": config_id, "file_version": version}


def test_unit_semantic_business_point_projection_chain(
    app: ConfigurationApplication,
) -> None:
    units = document_input(
        "units",
        "standard",
        "1.0.0",
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )
    rev1, (unit_version,) = publish_documents(app, (units,), request_id="typed-units")
    semantic = document_input(
        "semantic_points",
        "standard",
        "1.0.0",
        semantic_points={"active_power": {"data_type": "float", "unit": "watt"}},
        config_sources=[ref("units", "standard", "1.0.0")],
    )
    rev2, (semantic_version,) = publish_documents(
        app, (semantic,), expected_revision=rev1, request_id="typed-semantic"
    )
    business = document_input(
        "business_points",
        "wtg",
        "1.0.0",
        device_type="WTG",
        business_points={"power": {"semantic_point_id": "active_power"}},
        config_sources=[ref("semantic_points", "standard", "1.0.0")],
    )
    rev3, (business_version,) = publish_documents(
        app, (business,), expected_revision=rev2, request_id="typed-business"
    )
    point_table = document_input(
        "point_table",
        "wtg-modbus",
        "1.0.0",
        template_id="wtg-modbus",
        device_type="WTG",
        protocol="modbus",
        points={
            "power": {
                "business_point_id": "power",
                "data_type": "float",
                "unit": "watt",
                "address": {"kind": "holding", "address": 1},
            }
        },
        config_sources=[
            ref("business_points", "wtg", "1.0.0"),
            ref("units", "standard", "1.0.0"),
        ],
    )
    rev4, (point_version,) = publish_documents(
        app, (point_table,), expected_revision=rev3, request_id="typed-point"
    )
    assert (
        app.repository.get_projection_rows(unit_version, "unit_definitions")[0]["unit_id"] == "watt"
    )
    semantic_row = app.repository.get_projection_rows(semantic_version, "semantic_points")[0]
    assert semantic_row["standard_unit_version_id"] == unit_version
    business_row = app.repository.get_projection_rows(business_version, "business_points")[0]
    assert business_row["semantic_config_version_id"] == semantic_version
    point_row = app.repository.get_projection_rows(point_version, "point_mappings")[0]
    assert point_row["business_config_version_id"] == business_version
    assert point_row["source_unit_version_id"] == unit_version
    bundle = app.repository.get_bundle_versions((point_table.identity,), rev4, max_versions=10)
    assert [row["version_id"] for row in bundle] == [
        unit_version,
        semantic_version,
        business_version,
        point_version,
    ]


def test_ambiguous_two_unit_versions_cannot_bind_semantic_projection(
    app: ConfigurationApplication,
) -> None:
    units_v1 = document_input(
        "units",
        "standard",
        "1.0.0",
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )
    rev1, _ = publish_documents(app, (units_v1,), request_id="ambiguous-v1")
    units_v2 = document_input(
        "units",
        "standard",
        "2.0.0",
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )
    rev2, _ = publish_documents(app, (units_v2,), expected_revision=rev1, request_id="ambiguous-v2")
    semantic = document_input(
        "semantic_points",
        "standard",
        "1.0.0",
        semantic_points={"active_power": {"data_type": "float", "unit": "watt"}},
        config_sources=[
            ref("units", "standard", "1.0.0"),
            ref("units", "standard", "2.0.0"),
        ],
    )
    with pytest.raises(ValueError, match="units dependency"):
        app.create_candidate(
            actor_id="publisher",
            request_id="ambiguous-consumer",
            expected_revision=rev2,
            documents=(semantic,),
            validate_only=True,
        )


@pytest.mark.parametrize(
    ("relative_path", "table"),
    [
        ("configs/instances/devices.yaml", "devices"),
        (
            "configs/templates/point_tables/point_table_modbus_wtg_sim_v1.0.0.yaml",
            "point_tables",
        ),
    ],
)
def test_unbound_legacy_documents_keep_json_without_manufacturing_foreign_keys(
    app: ConfigurationApplication, relative_path: str, table: str
) -> None:
    item = input_for(ROOT / relative_path)
    _, (version_id,) = publish_documents(app, (item,), request_id=f"unbound-{table}")
    assert app.repository.get_projection_rows(version_id, table) == ()
    assert app.repository.get_projection_rows(version_id, "config_entries")


def test_full_device_point_dependency_chain_creates_composite_foreign_key_rows(
    app: ConfigurationApplication,
) -> None:
    units = document_input(
        "units",
        "standard",
        "1.0.0",
        quantities={"power": {"base_unit": "watt"}, "speed": {"base_unit": "meter_per_second"}},
        units={
            "watt": {"quantity": "power", "symbol": "W"},
            "meter_per_second": {"quantity": "speed", "symbol": "m/s"},
        },
    )
    semantic = document_input(
        "semantic_points",
        "turbine",
        "1.0.0",
        semantic_points={
            "active_power": {"data_type": "float", "unit": "watt"},
            "wind_speed": {"data_type": "float", "unit": "meter_per_second"},
        },
        config_sources=[ref("units", "standard", "1.0.0")],
    )
    business = document_input(
        "business_points",
        "turbine",
        "1.0.0",
        device_type="turbine",
        business_points={
            "active_power": {"semantic_point_id": "active_power"},
            "wind_speed": {"semantic_point_id": "wind_speed"},
        },
        config_sources=[ref("semantic_points", "turbine", "1.0.0")],
    )
    point_table = document_input(
        "point_table",
        "turbine-modbus",
        "1.0.0",
        template_id="turbine-modbus",
        device_type="turbine",
        protocol="modbus",
        points={
            "active_power": {
                "business_point_id": "active_power",
                "data_type": "float",
                "unit": "watt",
                "address": {"register_type": "holding", "address": 0},
            },
            "wind_speed": {
                "business_point_id": "wind_speed",
                "data_type": "float",
                "unit": "meter_per_second",
                "address": {"register_type": "input", "address": 2},
            },
        },
        config_sources=[
            ref("business_points", "turbine", "1.0.0"),
            ref("units", "standard", "1.0.0"),
        ],
    )
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
        "turbine-fleet",
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
        (units, semantic, business, point_table, model, device),
        request_id="full-device-chain",
    )
    (
        unit_version,
        semantic_version,
        business_version,
        table_version,
        model_version,
        device_version,
    ) = (
        app.repository.get_version(item.identity, revision_id=revision)["version_id"]
        for item in (units, semantic, business, point_table, model, device)
    )
    assert len(app.repository.get_projection_rows(unit_version, "unit_definitions")) == 2
    semantic_rows = app.repository.get_projection_rows(semantic_version, "semantic_points")
    assert {row["standard_unit_version_id"] for row in semantic_rows} == {unit_version}
    business_rows = app.repository.get_projection_rows(business_version, "business_points")
    assert {row["semantic_config_version_id"] for row in business_rows} == {semantic_version}
    table_rows = app.repository.get_projection_rows(table_version, "point_tables")
    assert len(table_rows) == 1
    assert table_rows[0]["point_table_id"] == "turbine-modbus"
    mapping_rows = app.repository.get_projection_rows(table_version, "point_mappings")
    assert len(mapping_rows) == 2
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
    units = document_input(
        "units",
        "standard",
        "1.0.0",
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )
    semantic = document_input(
        "semantic_points",
        "turbine",
        "1.0.0",
        semantic_points={"active_power": {"data_type": "float", "unit": "watt"}},
        config_sources=[ref("units", "standard", "1.0.0")],
    )
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="missing-dependency",
        expected_revision=0,
        documents=(units, semantic),
        validate_only=False,
    )
    broken = document_input(
        "business_points",
        "turbine",
        "1.0.0",
        device_type="turbine",
        business_points={"active_power": {"semantic_point_id": "missing"}},
        config_sources=[ref("semantic_points", "turbine", "9.9.9")],
    )
    with pytest.raises(RepositoryNotFoundError, match="referenced configuration version not found"):
        app.create_candidate(
            actor_id="publisher",
            request_id="broken-dependency",
            expected_revision=0,
            documents=(units, semantic, broken),
            validate_only=True,
        )
    assert app.repository.current_revision() == 0
    assert app.repository.get_candidate(candidate, "publisher")["status"] == "VALID"


def model_table_device(version: str):
    table = document_input(
        "point_table",
        "upgrade-table",
        version,
        template_id="upgrade-table",
        device_type="turbine",
        protocol="modbus",
        points={},
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
        "upgrade-fleet",
        version,
        devices={"TURBINE-01": {"model": {"id": "upgrade-model", "version": version}}},
    )
    return table, model, device


def test_model_upgrade_preserves_historical_device_bindings(
    app: ConfigurationApplication,
) -> None:
    revision1, ids1 = publish_documents(app, model_table_device("1.0.0"), request_id="model-v1")
    revision2, ids2 = publish_documents(
        app, model_table_device("2.0.0"), expected_revision=revision1, request_id="model-v2"
    )
    old_device = app.repository.get_projection_rows(ids1[-1], "devices")[0]
    new_device = app.repository.get_projection_rows(ids2[-1], "devices")[0]
    assert old_device["model_version_id"] == ids1[1]
    assert new_device["model_version_id"] == ids2[1]
    assert (
        app.get_config(model_table_device("1.0.0")[2].identity, revision_id=revision1).version_id
        == ids1[-1]
    )
    assert (
        app.get_config(model_table_device("2.0.0")[2].identity, revision_id=revision2).version_id
        == ids2[-1]
    )


def test_foreign_key_failure_inside_publish_rolls_back_every_document(
    app: ConfigurationApplication,
) -> None:
    documents = model_table_device("1.0.0")
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="fk-rollback-create",
        expected_revision=0,
        documents=documents,
        validate_only=False,
    )
    versions = list(app._compile_versions(prepared, 0))
    device_version = versions[-1]
    broken_projections = tuple(
        RowProjection(
            row.table,
            {**row.columns, "model_version_id": "missing-model-version"}
            if row.table == "devices"
            else row.columns,
        )
        for row in device_version.projections
    )
    versions[-1] = replace(device_version, projections=broken_projections)
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
