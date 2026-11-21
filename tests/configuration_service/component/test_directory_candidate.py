"""Full-directory candidate preparation, separate from SQLite publication."""

from __future__ import annotations

import pytest

from configuration_service.application.directory_manifest import DirectoryFile
from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import ConfigIdentity, RepositoryConflictError


def test_prepares_sorted_directory_with_pinned_file_versions(
    app: ConfigurationApplication,
) -> None:
    site = DirectoryFile(
        "instances/site.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\nsite_id: site-a\n",
    )
    table = DirectoryFile(
        "templates/point_tables/point_table_wtg_v1.0.0.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\n"
        b"template_id: wtg\nprotocol: modbus\npoints: {}\n",
    )
    manifest, prepared = app.prepare_directory((table, site))
    assert tuple(item.relative_path for item in manifest.files) == (
        "instances/site.yaml",
        "templates/point_tables/point_table_wtg_v1.0.0.yaml",
    )
    assert [(item.identity.config_type, item.identity.config_id) for item in prepared] == [
        ("site", "site"),
        ("point_table", "wtg"),
    ]
    assert manifest.total_bytes == len(site.yaml_bytes) + len(table.yaml_bytes)


def test_rejects_duplicate_logical_file_version(
    app: ConfigurationApplication,
) -> None:
    instance = DirectoryFile(
        "instances/sinks.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\nsinks: {}\n",
    )
    template = DirectoryFile(
        "templates/sinks/sink_sinks_v1.0.0.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\n" b"template_id: sinks\nkind: redis\n",
    )
    with pytest.raises(ValueError, match="duplicate configuration document identity"):
        app.prepare_directory((instance, template))


def test_rejects_configuration_service_bootstrap_in_full_root(
    app: ConfigurationApplication,
) -> None:
    bootstrap = DirectoryFile(
        "services/service_configuration_v1.0.0.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\n" b"service_id: configuration-main\n",
    )
    with pytest.raises(ValueError, match="bootstrap"):
        app.prepare_directory((bootstrap,))


def test_republishing_a_version_with_mutated_yaml_is_rejected(
    app: ConfigurationApplication,
) -> None:
    """Even comments cannot silently change the source of an immutable version."""
    payload = b"schema_version: 1\nfile_version: 1.0.0\nsite_id: site-a\n"
    identity = ConfigIdentity("site", "site")

    def stage(content: bytes, *, revision: int, request: str) -> str:
        candidate, _ = app.create_candidate(
            actor_id="publisher",
            request_id=f"{request}-stage",
            expected_revision=revision,
            documents=(
                DocumentInput(
                    identity=identity,
                    file_name="site.yaml",
                    yaml_bytes=content,
                    relative_path="instances/site.yaml",
                ),
            ),
            validate_only=False,
        )
        return candidate

    first_id = stage(payload, revision=0, request="first")
    first = app.publish_candidate(
        actor_id="publisher",
        request_id="first-publish",
        candidate_id=first_id,
        expected_revision=0,
    )
    source = app.repository.get_version_source(first.version_ids[0])
    assert source is not None
    assert source[1] == payload

    # Immutable source bytes (including comments) are rejected as early as
    # candidate staging, so no candidate id is ever issued for a redefined
    # file_version: validate_only and commit enforce the same violation.
    mutated = payload + b"# same semantic payload, new bytes\n"
    with pytest.raises(RepositoryConflictError, match="YAML bytes changed"):
        stage(
            mutated,
            revision=first.revision_id,
            request="changed",
        )
    with pytest.raises(RepositoryConflictError, match="YAML bytes changed"):
        app.create_candidate(
            actor_id="publisher",
            request_id="changed-dry-run",
            expected_revision=first.revision_id,
            documents=(
                DocumentInput(
                    identity=identity,
                    file_name="site.yaml",
                    yaml_bytes=mutated,
                    relative_path="instances/site.yaml",
                ),
            ),
            validate_only=True,
        )
    # The dry run must leave no candidate and no new revision behind.
    assert app.repository.current_revision() == first.revision_id
    assert app.repository.get_version_source(first.version_ids[0]) == source
