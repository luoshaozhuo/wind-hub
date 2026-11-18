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
