"""Service references only logical Instance identities, never repository paths."""

from __future__ import annotations

import pytest

from core.configuration.deployment import (
    INSTANCE_ROLE_TYPES,
    parse_deployment,
    service_instance_refs,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("identity", ["farm-a", "sim"])
def test_service_references_typed_logical_device_identity(identity: str) -> None:
    service = {"config_sources": {"instances": {"devices": identity}}}
    assert service_instance_refs(service, INSTANCE_ROLE_TYPES) == (("device", identity),)


@pytest.mark.parametrize(
    "identity",
    ["../../etc/passwd", "configs/instances/device_farm-a_v1.4.0.yaml", "", "/absolute"],
)
def test_service_rejects_path_or_unsafe_instance_identity(identity: str) -> None:
    with pytest.raises(ValueError, match="identity|invalid|must"):
        service_instance_refs(
            {"config_sources": {"instances": {"devices": identity}}},
            INSTANCE_ROLE_TYPES,
        )


def test_template_root_and_direct_version_are_forbidden() -> None:
    with pytest.raises(ValueError, match="instances only"):
        service_instance_refs(
            {
                "config_sources": {
                    "instances": {"devices": "farm-a"},
                    "templates": {"root": "configs/templates"},
                }
            },
            INSTANCE_ROLE_TYPES,
        )
    with pytest.raises(ValueError, match="identity|invalid"):
        service_instance_refs(
            {"config_sources": {"instances": {"devices": "farm-a@1.4.0"}}},
            INSTANCE_ROLE_TYPES,
        )


def test_deployment_selects_exact_device_version() -> None:
    manifest = parse_deployment(
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "deployment_id": "test",
            "services": {
                "gateway": {"config_id": "gateway", "file_version": "1.0.0"}
            },
            "instances": {
                "device": {"farm-a": {"file_version": "1.4.0"}}
            },
        }
    )
    assert manifest.instances[("device", "farm-a")].file_version == "1.4.0"
