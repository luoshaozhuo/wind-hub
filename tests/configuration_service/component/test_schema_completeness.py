"""Executable schema and full-Header design requirements."""

from __future__ import annotations

import pytest

from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import ConfigurationApplication
from tests.configuration_service.conftest import document_input

pytestmark = pytest.mark.component


def test_nested_module_schema_drives_complete_header() -> None:
    schemas = {item.config_type: item for item in iter_schemas()}
    for field in ("runtime", "grpc", "security", "repository"):
        definition = schemas["service"].header[field]
        assert definition["type"] == "object"
        assert definition.get(
            "properties"
        ), f"service.{field} lacks executable nested Header/schema properties"


def test_device_model_accepts_core_protocol_options(app: ConfigurationApplication) -> None:
    item = document_input(
        "device_model",
        "modbus_wtg",
        "1.0.0",
        template_id="modbus_wtg",
        name="Modbus turbine",
        device_type_id="turbine",
        protocol={"id": "modbus_tcp", "version": "1.1.0"},
        protocol_options={"slave_id": 1, "word_order": "big_endian"},
        point_table={"id": "modbus_wtg", "version": "1.0.0"},
        task={"id": "turbine_all", "version": "1.0.0"},
    )
    dependencies = (
        document_input(
            "protocol",
            "modbus_tcp",
            "1.1.0",
            template_id="modbus_tcp",
            protocol="modbus",
        ),
        document_input(
            "point_table",
            "modbus_wtg",
            "1.0.0",
            template_id="modbus_wtg",
            protocol="modbus",
            points={},
        ),
        document_input(
            "task",
            "turbine_all",
            "1.0.0",
            template_id="turbine_all",
            point_group="all",
        ),
    )
    candidate_id, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="device-model-protocol-options",
        expected_revision=0,
        documents=(item, *dependencies),
        validate_only=True,
    )
    assert candidate_id == ""
    assert len(prepared) == 4


def test_device_config_source_keeps_explicit_site_binding(
    app: ConfigurationApplication,
) -> None:
    service = document_input(
        "service",
        "storage",
        "1.0.0",
        service_id="storage-main",
        config_sources=[
            {
                "config_type": "device",
                "config_id": "devices",
                "file_version": "1.0.0",
                "site_config_id": "site",
                "site_id": "farm-a",
                "site_file_version": "1.0.0",
            },
            {"config_type": "site", "config_id": "site", "file_version": "1.0.0"},
        ],
    )
    site = document_input("site", "site", "1.0.0", site_id="farm-a", name="Farm A")
    devices = document_input("device", "devices", "1.0.0", devices={})
    candidate_id, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="device-source-site-binding",
        expected_revision=0,
        documents=(service, site, devices),
        validate_only=True,
    )
    assert candidate_id == ""
    assert len(prepared) == 3


def test_unknown_nested_service_field_is_rejected(app: ConfigurationApplication) -> None:
    item = document_input(
        "service",
        "collector",
        "1.0.0",
        runtime={"not_a_supported_runtime_option": True},
    )
    with pytest.raises(ValueError, match="unknown"):
        app.create_candidate(
            actor_id="publisher",
            request_id="unknown-nested",
            expected_revision=0,
            documents=(item,),
            validate_only=True,
        )
