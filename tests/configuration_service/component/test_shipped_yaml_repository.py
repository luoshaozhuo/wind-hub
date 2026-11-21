"""YAML -> SQLite -> YAML acceptance for every shipped repository document."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cli.configuration_directory import read_complete_directory
from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import ConfigIdentity
from configuration_service.infrastructure.adapters.outbound.yaml.codec import (
    canonical_json,
    parse_yaml,
)
from tests.configuration_service.unit.test_yaml_acceptance import ROOT, YAML_FILES, identity_for

pytestmark = pytest.mark.component
BOOTSTRAP = ROOT / "deploy/configuration/service_configuration_v1.1.0.yaml"
STORAGE_BOOTSTRAP = ROOT / "configs/services/storage_bootstrap_v1.0.0.yaml"
REPOSITORY_YAMLS = tuple(path for path in YAML_FILES if path not in {BOOTSTRAP, STORAGE_BOOTSTRAP})
DIRECTORY_INPUTS = tuple(
    DocumentInput(
        identity=ConfigIdentity(item.config_type, item.config_id),
        file_name=item.filename,
        yaml_bytes=item.content,
    )
    for item in read_complete_directory(ROOT / "configs")
)


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
    selected = any(
        candidate.identity == item.identity and candidate.yaml_bytes == item.yaml_bytes
        for candidate in DIRECTORY_INPUTS
    )
    if selected:
        documents = DIRECTORY_INPUTS
    elif path.parent == ROOT / "configs/services":
        documents = (*DIRECTORY_INPUTS, item)
    else:
        documents = (item,)
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
    app.publish_candidate(
        actor_id="publisher",
        request_id=f"publish-{path}",
        candidate_id=candidate_id,
        expected_revision=0,
    )
    original = parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)
    exact_version = app.repository.resolve_version(item.identity, str(original["file_version"]))
    restored = app.get_config(item.identity, version_id=exact_version)
    reparsed = parse_yaml(restored.yaml_bytes, max_bytes=64 * 1024 * 1024)
    assert canonical_json(reparsed) == canonical_json(original)
    prepared_item = next(
        document
        for document in validated
        if document.identity == item.identity and document.file_version == original["file_version"]
    )
    assert restored.content_sha256 == prepared_item.content_sha256
    assert reparsed == original
    assert restored.yaml_bytes == path.read_bytes()
    assert restored.yaml_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


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
