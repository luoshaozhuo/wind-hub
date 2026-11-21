"""Nested schema, legacy template roots and immutable registration regressions."""

from __future__ import annotations

import pytest
import yaml

from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import RepositoryConflictError
from configuration_service.infrastructure.adapters.outbound.yaml.codec import emit_yaml, parse_yaml
from tests.configuration_service.component.test_shipped_yaml_repository import (
    input_for,
    original_file_version,
)
from tests.configuration_service.conftest import document_input, publish_documents
from tests.configuration_service.unit.test_yaml_acceptance import ROOT, YAML_FILES

pytestmark = pytest.mark.component


def test_existing_commander_caller_array_is_admitted_and_reconstructed(
    app: ConfigurationApplication,
) -> None:
    # Full-directory publication admits the shipped root as one complete
    # closure; the nested caller array survives as frozen, parseable YAML.
    documents = tuple(input_for(path) for path in YAML_FILES)
    revision, _ = publish_documents(app, documents, request_id="caller-array")
    item = input_for(ROOT / "configs/services/service_commander_v1.4.0.yaml")
    version_id = app.repository.resolve_version(
        item.identity, original_file_version(ROOT / "configs/services/service_commander_v1.4.0.yaml")
    )
    restored = app.get_config(item.identity, version_id=version_id)
    source = (ROOT / "configs/services/service_commander_v1.4.0.yaml").read_bytes()
    assert isinstance(restored.yaml_bytes, bytes)
    assert restored.yaml_bytes == source
    reparsed = parse_yaml(restored.yaml_bytes, max_bytes=64 * 1024 * 1024)
    assert (
        reparsed["security"]["callers"]
        == parse_yaml(source, max_bytes=64 * 1024 * 1024)["security"]["callers"]
    )
    assert revision > 0


@pytest.mark.parametrize("root", ["configs/other", "../configs/templates", "/configs/templates"])
def test_invalid_legacy_template_root_is_rejected(app: ConfigurationApplication, root: str) -> None:
    item = document_input(
        "service",
        "unsafe-root",
        "1.0.0",
        config_sources={"templates": {"root": root}},
    )
    with pytest.raises((ValueError, RepositoryConflictError), match="root|templates"):
        app.create_candidate(
            actor_id="publisher",
            request_id=f"unsafe-root-{root}",
            expected_revision=0,
            documents=(item,),
            validate_only=True,
        )


def register_nested_schema(app: ConfigurationApplication) -> None:
    # service schema v2 is now the built-in token-security contract; use a free
    # version slot to exercise the generic nested-array validator without
    # colliding with an immutable registered definition.
    app.register_schema(
        config_type="service",
        document_schema_version=4,
        fields={
            "type": "object",
            "properties": {
                "schema_version": {"type": "integer"},
                "file_version": {"type": "string"},
                "security": {
                    "type": "object",
                    "required": ["callers"],
                    "additionalProperties": False,
                    "properties": {
                        "callers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["caller_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "caller_id": {"type": "string"},
                                    "permissions": {
                                        "type": "array",
                                        "items": {"type": "string", "enum": ["query"]},
                                    },
                                },
                            },
                        }
                    },
                },
            },
            "required": ["schema_version", "file_version", "security"],
            "additionalProperties": False,
        },
        header={},
    )


def nested_service_item(caller: dict[str, object]) -> DocumentInput:
    item = document_input("service", "nested", "1.0.0", security={"callers": [caller]})
    payload = yaml.safe_load(item.yaml_bytes)
    payload["schema_version"] = 4
    return type(item)(
        identity=item.identity,
        file_name=item.file_name,
        yaml_bytes=yaml.safe_dump(payload).encode(),
        relative_path=item.relative_path,
    )


def test_schema_validator_accepts_valid_array_items(app: ConfigurationApplication) -> None:
    register_nested_schema(app)
    item = nested_service_item({"caller_id": "viewer", "permissions": ["query"]})
    candidate_id, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="valid-nested-array-schema",
        expected_revision=0,
        documents=(item,),
        validate_only=True,
    )
    assert candidate_id == ""
    assert len(prepared) == 1


@pytest.mark.parametrize(
    ("caller", "reason"),
    [
        ({"permissions": ["query"]}, "caller_id"),
        ({"caller_id": "viewer", "unexpected": True}, "unexpected"),
        ({"caller_id": "viewer", "permissions": ["admin"]}, "enum"),
        ({"caller_id": "viewer", "permissions": {"query": True}}, "type"),
    ],
)
def test_schema_validator_enforces_array_item_contract(
    app: ConfigurationApplication, caller: dict[str, object], reason: str
) -> None:
    register_nested_schema(app)
    with pytest.raises(ValueError, match=reason):
        app.create_candidate(
            actor_id="publisher",
            request_id=f"invalid-nested-array-{reason}",
            expected_revision=0,
            documents=(nested_service_item(caller),),
            validate_only=True,
        )


def test_array_schema_without_items_fails_closed(app: ConfigurationApplication) -> None:
    app.register_schema(
        config_type="service",
        document_schema_version=3,
        fields={
            "properties": {
                "schema_version": {"type": "integer"},
                "file_version": {"type": "string"},
                "servers": {"type": "array"},
            },
            "required": ["schema_version", "file_version", "servers"],
            "additionalProperties": False,
        },
        header={},
    )
    item = document_input("service", "untyped-array", "1.0.0", servers=[123])
    payload = yaml.safe_load(item.yaml_bytes)
    payload["schema_version"] = 3
    item = type(item)(
        identity=item.identity,
        file_name=item.file_name,
        yaml_bytes=yaml.safe_dump(payload).encode(),
        relative_path=item.relative_path,
    )
    with pytest.raises(ValueError, match="items"):
        app.create_candidate(
            actor_id="publisher",
            request_id="array-without-items",
            expected_revision=0,
            documents=(item,),
            validate_only=True,
        )


def test_caller_header_declares_complete_required_item_fields() -> None:
    schema = next(item for item in iter_schemas() if item.config_type == "service")
    document = parse_yaml(
        (ROOT / "configs/services/service_commander_v1.4.0.yaml").read_bytes(),
        max_bytes=64 * 1024 * 1024,
    )
    exported = emit_yaml(document, schema.header).decode()
    for field in ("caller_id", "certificate_identity", "permissions"):
        assert f"# security.callers.<item>.{field}: 必填" in exported


def test_identical_schema_registration_is_idempotent(app: ConfigurationApplication) -> None:
    schema = next(item for item in iter_schemas() if item.config_type == "site")
    first = app.register_schema(
        config_type=schema.config_type,
        document_schema_version=schema.schema_version,
        fields=schema.fields,
        header=schema.header,
    )
    second = app.register_schema(
        config_type=schema.config_type,
        document_schema_version=schema.schema_version,
        fields=schema.fields,
        header=schema.header,
    )
    assert first == second


def test_schema_registration_conflict_is_typed_and_fail_closed(
    app: ConfigurationApplication,
) -> None:
    with pytest.raises(RepositoryConflictError):
        app.register_schema(
            config_type="site",
            document_schema_version=1,
            fields={
                "properties": {"schema_version": {"type": "integer"}},
                "required": ["schema_version"],
                "additionalProperties": False,
            },
            header={},
        )
