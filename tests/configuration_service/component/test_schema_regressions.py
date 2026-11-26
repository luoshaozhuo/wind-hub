"""Nested schema, legacy template roots and immutable registration regressions."""

from __future__ import annotations

import pytest
import yaml

from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import RepositoryConflictError
from configuration_service.infrastructure.adapters.outbound.yaml.codec import emit_yaml, parse_yaml
from tests.configuration_service.component.test_shipped_yaml_repository import (
    ACTIVE_ROOTS,
    input_for,
    original_file_version,
    publish_full_root,
)
from tests.configuration_service.conftest import document_input
from tests.configuration_service.unit.test_yaml_acceptance import ROOT

pytestmark = pytest.mark.component


def test_existing_commander_caller_array_is_admitted_and_reconstructed(
    app: ConfigurationApplication,
) -> None:
    # 每个活动根以完整目录闭包发布；调用方数组作为冻结、可解析的 YAML 保留。
    revision = 0
    for root_name, root in ACTIVE_ROOTS:
        revision, _ = publish_full_root(
            app, root, tag=f"caller-array-{root_name}", expected_revision=revision
        )
    commander = ROOT / "deploy/config_roots/prod-a/services/service_commander_v1.5.0.yaml"
    item = input_for(commander, ROOT / "deploy/config_roots/prod-a")
    version_id = app.repository.resolve_version(
        item.identity,
        original_file_version(commander),
    )
    restored = app.get_config(item.identity, version_id=version_id)
    source = commander.read_bytes()
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
    with pytest.raises(
        (ValueError, RepositoryConflictError), match="root|templates|instances only"
    ):
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
                "service_id": {"type": "string"},
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
        document_schema_version=5,
        fields={
            "properties": {
                "schema_version": {"type": "integer"},
                "file_version": {"type": "string"},
                "service_id": {"type": "string"},
                "servers": {"type": "array"},
            },
            "required": ["schema_version", "file_version", "servers"],
            "additionalProperties": False,
        },
        header={},
    )
    item = document_input("service", "untyped-array", "1.0.0", servers=[123])
    payload = yaml.safe_load(item.yaml_bytes)
    payload["schema_version"] = 5
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


def test_service_v3_is_new_and_does_not_mutate_v2() -> None:
    """A new token-only contract must not rewrite published schema identities."""
    schemas = {
        item.schema_version: item for item in iter_schemas() if item.config_type == "service"
    }
    assert set(schemas) == {1, 2, 3, 6}
    assert (
        "certificate_identity"
        in schemas[1].fields["properties"]["security"]["properties"]["callers"]["items"][
            "properties"
        ]
    )
    v2 = schemas[2].fields["properties"]
    v3 = schemas[3].fields["properties"]
    assert "sink_listener_port" not in v2["grpc"]["properties"]
    assert "simulation_targets" not in v2
    assert "sink_listener_port" in v3["grpc"]["properties"]
    assert "tls" not in v3["grpc"]["properties"]
    assert "ca_cert" not in v3["security"]["properties"]
    assert "token_env" in v3["security"]["properties"]["callers"]["items"]["required"]


def test_service_v6_preserves_legacy_schemas_and_restricts_tokens() -> None:
    schemas = {
        item.schema_version: item for item in iter_schemas() if item.config_type == "service"
    }
    v3 = schemas[3].fields["properties"]
    v6 = schemas[6].fields["properties"]
    assert schemas[3].schema_version == 3
    assert "tls" not in v6["grpc"]["properties"]
    assert v6["security"]["properties"]["callers"]["items"]["properties"]["token_env"] == {
        "type": "string",
        "pattern": r"env:[A-Z_][A-Z0-9_]*",
    }
    assert v3["security"]["properties"]["callers"]["items"]["properties"]["token_env"] == {
        "type": "string",
        "pattern": r"[A-Z_][A-Z0-9_]*",
    }
    assert schemas[6].fields["additionalProperties"] is False
    assert "service_id" in schemas[6].fields["required"]


@pytest.mark.parametrize("token", ["TOKEN", "env:lowercase", "env:1BAD"])
def test_service_v6_rejects_invalid_token_references(token: str) -> None:
    schema = next(
        item.fields for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 6
    )
    with pytest.raises(ValueError):
        ConfigurationApplication._validate_schema(
            {
                "schema_version": 6,
                "file_version": "1.0.0",
                "service_id": "storage",
                "security": {
                    "callers": [
                        {
                            "caller_id": "storage-reader",
                            "token_env": token,
                            "permissions": ["query"],
                        }
                    ]
                },
            },
            schema,
        )


@pytest.mark.parametrize(
    "config_name",
    [
        "service_storage-main_v2.0.0.yaml",
        "service_collector-reliable_v2.0.0.yaml",
        "service_sink-west_v2.0.0.yaml",
    ],
)
def test_shipped_service_v6_schema_acceptance(config_name: str) -> None:
    raw = parse_yaml(
        (ROOT / "configs/services" / config_name).read_bytes(),
        max_bytes=64 * 1024 * 1024,
    )
    assert raw["schema_version"] == 6
    spec = next(
        item.fields for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 6
    )
    ConfigurationApplication._validate_schema(raw, spec)


def test_service_v3_accepts_strict_simulation_mapping() -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    ConfigurationApplication._validate_schema(
        {
            "schema_version": 3,
            "file_version": "1.1.0",
            "service_id": "commander-e2e",
            "simulation_targets": {
                "PPC_SIM01": {
                    "gateway_id": "gw-ppc-sim01",
                    "endpoint": "127.0.0.1:52073",
                    "token_env": "E2E_GATEWAY_TOKEN",
                }
            },
        },
        schema,
    )


@pytest.mark.parametrize(
    "bad_target",
    [
        {"gateway_id": "gw-ppc-sim01", "endpoint": "127.0.0.1:52073"},
        {"gateway_id": "gw-ppc-sim01", "endpoint": 52073, "token_env": "ENV"},
        {
            "gateway_id": "gw-ppc-sim01",
            "endpoint": "127.0.0.1:52073",
            "token_env": "ENV",
            "client_cert": "/tmp/legacy.pem",
        },
    ],
)
def test_service_v3_rejects_invalid_simulation_mapping(
    bad_target: dict[str, object],
) -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    with pytest.raises(ValueError):
        ConfigurationApplication._validate_schema(
            {
                "schema_version": 3,
                "file_version": "1.1.0",
                "simulation_targets": {"PPC_SIM01": bad_target},
            },
            schema,
        )


@pytest.mark.parametrize(
    "grpc",
    [
        {"listen_port": 53073, "sink_listener_port": "53074"},
        {"listen_port": 53073, "tls": {"enabled": False}},
    ],
)
def test_service_v3_rejects_obsolete_or_mistyped_grpc(
    grpc: dict[str, object],
) -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    with pytest.raises(ValueError):
        ConfigurationApplication._validate_schema(
            {"schema_version": 3, "file_version": "1.1.0", "grpc": grpc},
            schema,
        )


@pytest.mark.parametrize(
    "config_name",
    [
        "service_commander-e2e_v1.1.0.yaml",
        "service_gateway-e2e-source_v1.1.2.yaml",
        "service_gateway-e2e-target_v1.1.1.yaml",
    ],
)
def test_shipped_e2e_v3_schema_acceptance(config_name: str) -> None:
    """Unissued full-chain candidates have no obsolete security schema fields."""
    raw = parse_yaml(
        (ROOT / "configs/services" / config_name).read_bytes(),
        max_bytes=64 * 1024 * 1024,
    )
    assert raw["schema_version"] == 3
    spec = next(
        item
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    ConfigurationApplication._validate_schema(raw, spec.fields)


def test_service_v3_rejects_duplicate_grpc_authorization_source() -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    with pytest.raises(ValueError, match="unknown"):
        ConfigurationApplication._validate_schema(
            {
                "schema_version": 3,
                "file_version": "1.1.0",
                "grpc": {"permissions": {"operator": ["read"]}},
                "security": {
                    "callers": [
                        {
                            "caller_id": "operator",
                            "token_env": "OPERATOR_TOKEN",
                            "permissions": ["read"],
                        }
                    ]
                },
            },
            schema,
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("sink_listener_port", 0),
        ("sink_listener_port", 65536),
        ("sink_listener_port", True),
    ],
)
def test_service_v3_rejects_invalid_sink_listener_port(field: str, invalid: object) -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    with pytest.raises(ValueError):
        ConfigurationApplication._validate_schema(
            {
                "schema_version": 3,
                "file_version": "1.1.1",
                "grpc": {field: invalid},
            },
            schema,
        )


@pytest.mark.parametrize(
    "token_reference",
    ["", "lowercase", "env:LEGACY_TOKEN", "TOKEN WITH SPACE", 123],
)
def test_service_v3_rejects_invalid_token_environment_name(
    token_reference: object,
) -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    with pytest.raises(ValueError):
        ConfigurationApplication._validate_schema(
            {
                "schema_version": 3,
                "file_version": "1.1.0",
                "security": {
                    "callers": [
                        {
                            "caller_id": "operator",
                            "token_env": token_reference,
                            "permissions": ["query"],
                        }
                    ]
                },
            },
            schema,
        )


@pytest.mark.parametrize("endpoint", ["", "127.0.0.1", "127.0.0.1:abc", " :53073"])
def test_service_v3_rejects_malformed_target_endpoint(endpoint: str) -> None:
    schema = next(
        item.fields
        for item in iter_schemas()
        if item.config_type == "service" and item.schema_version == 3
    )
    with pytest.raises(ValueError):
        ConfigurationApplication._validate_schema(
            {
                "schema_version": 3,
                "file_version": "1.1.0",
                "simulation_targets": {
                    "PPC_SIM01": {
                        "gateway_id": "gw-ppc-sim01",
                        "endpoint": endpoint,
                        "token_env": "WIND_HUB_GATEWAY_TOKEN",
                    }
                },
            },
            schema,
        )
