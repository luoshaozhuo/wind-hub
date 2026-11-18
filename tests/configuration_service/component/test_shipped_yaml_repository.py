"""YAML -> SQLite -> YAML acceptance for every shipped repository document."""

from __future__ import annotations

from pathlib import Path

import pytest

from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import ConfigIdentity
from configuration_service.infrastructure.adapters.outbound.yaml.codec import (
    canonical_json,
    parse_yaml,
)
from tests.configuration_service.unit.test_yaml_acceptance import ROOT, YAML_FILES, identity_for

pytestmark = pytest.mark.component
BOOTSTRAP = ROOT / "configs/services/service_configuration_v1.0.0.yaml"
REPOSITORY_YAMLS = tuple(path for path in YAML_FILES if path != BOOTSTRAP)


def input_for(path: Path) -> DocumentInput:
    kind, config_id = identity_for(path)
    return DocumentInput(
        identity=ConfigIdentity(kind, config_id),
        file_name=path.name,
        yaml_bytes=path.read_bytes(),
    )


@pytest.mark.parametrize("path", REPOSITORY_YAMLS, ids=lambda path: str(path.relative_to(ROOT)))
def test_shipped_yaml_sqlite_yaml_roundtrip(app: ConfigurationApplication, path: Path) -> None:
    item = input_for(path)
    dependencies_by_identity: dict[ConfigIdentity, DocumentInput] = {}
    if "config_sources" in parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024):
        for candidate in REPOSITORY_YAMLS:
            if candidate != path and candidate.parent.parent.name != "services":
                dependency = input_for(candidate)
                dependencies_by_identity[dependency.identity] = dependency
    documents = (*dependencies_by_identity.values(), item)
    validated_id, validated = app.create_candidate(
        actor_id="publisher",
        request_id=f"validate-{path}",
        expected_revision=0,
        documents=documents,
        validate_only=True,
    )
    assert validated_id == ""
    assert len(validated) == len(documents)
    candidate_id, _ = app.create_candidate(
        actor_id="publisher",
        request_id=f"stage-{path}",
        expected_revision=0,
        documents=documents,
        validate_only=False,
    )
    committed = app.publish_candidate(
        actor_id="publisher",
        request_id=f"publish-{path}",
        candidate_id=candidate_id,
        expected_revision=0,
    )
    restored = app.get_config(item.identity, revision_id=committed.revision_id)
    original = parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)
    reparsed = parse_yaml(restored.yaml_bytes, max_bytes=64 * 1024 * 1024)
    assert canonical_json(reparsed) == canonical_json(original)
    assert restored.content_sha256 == validated[-1].content_sha256
    assert reparsed == original
    assert all(f"# {field}:".encode() in restored.yaml_bytes for field in original)


def test_own_bootstrap_cannot_be_admitted_to_repository(
    app: ConfigurationApplication,
) -> None:
    path = BOOTSTRAP
    item = DocumentInput(
        identity=ConfigIdentity("service", "configuration-test"),
        file_name=path.name,
        yaml_bytes=path.read_bytes(),
    )
    with pytest.raises(ValueError, match="bootstrap must remain local"):
        app.create_candidate(
            actor_id="publisher",
            request_id="own-bootstrap",
            expected_revision=0,
            documents=(item,),
            validate_only=True,
        )
