"""YAML -> SQLite -> YAML acceptance for every document in an active config root.

v1_16 全目录语义按**单个 Deployment 活动根**发布（``deploy/config_roots/<root>``，
每根 ≤ 35 份）。``configs/`` 是不可变历史归档，含多个历史 Deployment 版本与
已被后继取代的畸形候选，不能作为一次发布单元；活动根才是生产发布边界。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.configuration_commands import _is_local_bootstrap
from configuration_service.application.service import (
    ConfigurationApplication,
    DocumentInput,
)
from configuration_service.domain.models import ConfigIdentity
from configuration_service.infrastructure.adapters.outbound.yaml.codec import (
    canonical_json,
    parse_yaml,
)
from core.configuration.publication_identity import resolve_publication_identity
from tests.configuration_service.unit.test_yaml_acceptance import ROOT

pytestmark = pytest.mark.component
BOOTSTRAP = ROOT / "deploy/configuration/service_configuration_v1.1.0.yaml"
ACTIVE_ROOTS = tuple(
    (name, ROOT / "deploy" / "config_roots" / name) for name in ("prod-a", "sim-a", "e2e-final")
)
REPOSITORY_YAMLS = tuple(
    (root_name, path)
    for root_name, root in ACTIVE_ROOTS
    for path in sorted(root.rglob("*.yaml"))
    if not _is_local_bootstrap(path.relative_to(root).as_posix())
)
REPOSITORY_IDS = tuple(
    f"{root_name}/{path.relative_to(ROOT / 'deploy' / 'config_roots' / root_name)}"
    for root_name, path in REPOSITORY_YAMLS
)


def original_file_version(path: Path) -> str:
    raw = parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)
    return str(raw["file_version"])


def input_for(path: Path, root: Path) -> DocumentInput:
    relative_path = path.relative_to(root).as_posix()
    raw = parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)
    location = resolve_publication_identity(relative_path, raw)
    return DocumentInput(
        identity=ConfigIdentity(location.config_type, location.config_id),
        file_name=path.name,
        yaml_bytes=path.read_bytes(),
        relative_path=relative_path,
    )


def documents_for(root: Path) -> tuple[DocumentInput, ...]:
    return tuple(
        input_for(path, root)
        for path in sorted(root.rglob("*.yaml"))
        if not _is_local_bootstrap(path.relative_to(root).as_posix())
    )


def publish_full_root(
    app: ConfigurationApplication,
    root: Path,
    *,
    tag: str,
    expected_revision: int = 0,
) -> tuple[int, dict[tuple[ConfigIdentity, str], str]]:
    """Validate and publish one active root's complete directory in one revision."""
    documents = documents_for(root)
    validated_id, validated = app.create_candidate(
        actor_id="publisher",
        request_id=f"validate-{tag}",
        expected_revision=expected_revision,
        documents=documents,
        validate_only=True,
    )
    assert validated_id == ""
    assert len(validated) == len(documents)
    validated_sha = {
        (document.identity, document.file_version): document.content_sha256
        for document in validated
    }
    candidate_id, _ = app.create_candidate(
        actor_id="publisher",
        request_id=f"stage-{tag}",
        expected_revision=expected_revision,
        documents=documents,
        validate_only=False,
    )
    committed = app.publish_candidate(
        actor_id="publisher",
        request_id=f"publish-{tag}",
        candidate_id=candidate_id,
        expected_revision=expected_revision,
    )
    return committed.revision_id, validated_sha


@pytest.mark.parametrize(("root_name", "path"), REPOSITORY_YAMLS, ids=REPOSITORY_IDS)
def test_shipped_yaml_sqlite_yaml_roundtrip(
    app: ConfigurationApplication, root_name: str, path: Path
) -> None:
    root = ROOT / "deploy" / "config_roots" / root_name
    item = input_for(path, root)
    file_version = str(original_file_version(path))
    revision_id, validated_sha = publish_full_root(app, root, tag=root_name)
    assert revision_id > 0
    # Several shipped identities carry multiple file_versions in one root;
    # select the exact immutable version instead of the revision's recommended
    # member so every shipped file is compared against its own stored bytes.
    version_id = app.repository.resolve_version(item.identity, file_version)
    restored = app.get_config(item.identity, version_id=version_id)
    assert restored.content_sha256 == validated_sha[(item.identity, file_version)]
    source_bytes = path.read_bytes()
    original = parse_yaml(source_bytes, max_bytes=64 * 1024 * 1024)
    # Full-directory publication freezes the submitted YAML bytes as an
    # immutable historical artifact: no re-emission or header rewriting.
    assert restored.yaml_bytes == source_bytes
    reparsed = parse_yaml(restored.yaml_bytes, max_bytes=64 * 1024 * 1024)
    assert canonical_json(reparsed) == canonical_json(original)
    assert reparsed == original


def test_own_bootstrap_cannot_be_admitted_to_repository(
    app: ConfigurationApplication,
) -> None:
    path = BOOTSTRAP
    # Rebase the real shipped bootstrap document onto the test application's
    # own service id so publication identity resolution succeeds and the
    # admission guard itself is exercised (local bootstrap stays outside the
    # repository, regardless of its service id).
    source_bytes = path.read_bytes().replace(
        b"service_id: configuration-main",
        b"service_id: configuration-test",
    )
    item = DocumentInput(
        identity=ConfigIdentity("service", "configuration-test"),
        file_name="service_configuration-test_v1.1.0.yaml",
        yaml_bytes=source_bytes,
        relative_path="services/service_configuration-test_v1.1.0.yaml",
    )
    with pytest.raises(ValueError, match="bootstrap"):
        app.create_candidate(
            actor_id="publisher",
            request_id="own-bootstrap",
            expected_revision=0,
            documents=(item,),
            validate_only=True,
        )
