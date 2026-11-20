"""revision_files 多版本目录与 v1→v2 原子迁移验收。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import ConfigurationApplication, RepositoryLimits
from configuration_service.domain.models import (
    ConfigIdentity,
    PreparedDocument,
    RepositoryNotFoundError,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.backup import SQLiteBackupManager
from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.schema import (
    INITIAL_MIGRATION_ID,
    LEGACY_IMMUTABLE_TABLES,
    MIGRATION_ID,
    SCHEMA_VERSION,
    V1_DDL,
    _immutable_trigger_sql,
    _schema_signature,
)
from configuration_service.infrastructure.adapters.outbound.yaml.codec import (
    canonical_json,
    semantic_hash,
)
from configuration_service.infrastructure.adapters.outbound.yaml.document_codec import (
    YamlDocumentCodec,
)
from tests.configuration_service.conftest import document_input, publish_documents

pytestmark = pytest.mark.component


def test_revision_files_keep_all_versions_and_original_yaml(
    app: ConfigurationApplication,
) -> None:
    root_v1 = document_input("site", "root", "1.0.0", name="root-v1")
    root_v2 = document_input("site", "root", "2.0.0", name="root-v2")
    consumer = document_input(
        "site",
        "consumer",
        "1.0.0",
        config_sources=[{"config_type": "site", "config_id": "root", "file_version": "1.0.0"}],
    )
    revision, _ = publish_documents(app, (root_v1, root_v2, consumer), request_id="multi-version")
    files = app.repository.get_revision_files(revision)
    assert len(files) == 3
    assert {item["version_id"] for item in files} == set(
        app.repository.get_revision_members(revision).values()
    ) | {app.repository.resolve_version(root_v2.identity, "2.0.0")}
    members = app.repository.get_revision_members(revision)
    assert members[root_v1.identity] == app.repository.resolve_version(root_v1.identity, "1.0.0")
    assert app.repository.resolve_version(root_v2.identity, "2.0.0") not in members.values()
    for item in files:
        assert item["relative_path"]
        assert hashlib.sha256(item["yaml_bytes"]).hexdigest() == item["yaml_sha256"]
    historical = app.get_config(
        root_v2.identity,
        version_id=app.repository.resolve_version(root_v2.identity, "2.0.0"),
    )
    assert historical.file_version == "2.0.0"
    with pytest.raises(RepositoryNotFoundError):
        app.repository.get_revision_files(999)


def test_independent_references_can_leave_identity_without_recommendation(
    app: ConfigurationApplication,
) -> None:
    root_v1 = document_input("site", "shared", "1.0.0")
    root_v2 = document_input("site", "shared", "2.0.0")
    consumer_v1 = document_input(
        "site",
        "consumer-v1",
        "1.0.0",
        config_sources=[{"config_type": "site", "config_id": "shared", "file_version": "1.0.0"}],
    )
    consumer_v2 = document_input(
        "site",
        "consumer-v2",
        "1.0.0",
        config_sources=[{"config_type": "site", "config_id": "shared", "file_version": "2.0.0"}],
    )
    revision, _ = publish_documents(
        app,
        (root_v1, root_v2, consumer_v1, consumer_v2),
        request_id="independent-version-references",
    )
    assert len(app.repository.get_revision_files(revision)) == 4
    assert root_v1.identity not in app.repository.get_revision_members(revision)


def test_legacy_instance_reference_uses_filename_version(
    app: ConfigurationApplication,
) -> None:
    sink_v1 = document_input("sink", "kafka_history", "1.0.0", template_id="kafka_history")
    sink_v2 = document_input("sink", "kafka_history", "2.0.0", template_id="kafka_history")
    instance_v1 = document_input(
        "sink",
        "sinks",
        "1.0.0",
        sinks={"history": {"template_id": "kafka_history", "template_version": "1.0.0"}},
    )
    instance_v2 = document_input(
        "sink",
        "sinks",
        "2.0.0",
        sinks={"history": {"template_id": "kafka_history", "template_version": "2.0.0"}},
    )
    service = document_input(
        "service",
        "sink-service",
        "1.0.0",
        config_sources={
            "instances": {
                "sinks": "configs/instances/sink_sinks_v1.0.0.yaml",
            },
            "templates": {"root": "configs/templates"},
        },
    )
    revision, _ = publish_documents(
        app,
        (sink_v1, sink_v2, instance_v1, instance_v2, service),
        request_id="legacy-versioned-path",
    )
    service_version = app.repository.resolve_version(service.identity, "1.0.0")
    references = app.repository.get_version_references(service_version)
    assert references == (
        (app.repository.resolve_version(instance_v1.identity, "1.0.0"), "config_sources.instances"),
    )
    assert len(app.repository.get_revision_files(revision)) == 5

    ambiguous = document_input(
        "service",
        "ambiguous-service",
        "1.0.0",
        config_sources={
            "instances": {"sinks": "configs/instances/sink_sinks.yaml"},
            "templates": {"root": "configs/templates"},
        },
    )
    with pytest.raises(Exception, match="ambiguous"):
        app.create_candidate(
            actor_id="publisher",
            request_id="ambiguous-unversioned-path",
            expected_revision=revision,
            documents=(sink_v1, sink_v2, instance_v1, instance_v2, ambiguous),
            validate_only=True,
        )


def _create_v1_database(path: Path, *, corrupt: bool = False) -> None:
    specification = next(item for item in iter_schemas() if item.config_type == "site")
    payload = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "site_id": "legacy",
        "name": "legacy-name",
    }
    document_id = hashlib.sha256(b"site\x00legacy").hexdigest()
    version_id = "legacy-version-id"
    with sqlite3.connect(path) as connection:
        for statement in V1_DDL.split(";"):
            if statement.strip():
                connection.execute(statement)
        for table in LEGACY_IMMUTABLE_TABLES:
            for operation in ("UPDATE", "DELETE"):
                connection.execute(_immutable_trigger_sql(table, operation))
        now = "2026-01-01T00:00:00+00:00"
        connection.execute(
            "INSERT INTO db_meta VALUES (1, 1, ?, ?, ?)",
            (INITIAL_MIGRATION_ID, now, "test"),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 0, 1, ?, ?)",
            (INITIAL_MIGRATION_ID, now, _schema_signature(V1_DDL)),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO config_schemas VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "site:1:legacy",
                "site",
                1,
                canonical_json(specification.fields),
                canonical_json(specification.header),
                "0" * 64,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO config_documents VALUES (?, 'site', 'legacy')", (document_id,)
        )
        connection.execute(
            "INSERT INTO config_versions VALUES (?, ?, ?, '1.0.0', ?, ?)",
            (version_id, document_id, "site:1:legacy", semantic_hash(payload), now),
        )
        connection.execute(
            "INSERT INTO revisions VALUES (1, NULL, 'legacy', 'legacy-request', ?)", (now,)
        )
        connection.execute(
            "INSERT INTO revision_members VALUES (1, ?, ?)", (document_id, version_id)
        )
        if not corrupt:
            connection.execute(
                "INSERT INTO config_entries VALUES (?, 'document', 'root', ?)",
                (version_id, canonical_json(payload)),
            )
            other = {**payload, "file_version": "2.0.0", "name": "legacy-other"}
            connection.execute(
                "INSERT INTO config_versions VALUES ('legacy-other-version', ?, ?, '2.0.0', ?, ?)",
                (document_id, "site:1:legacy", semantic_hash(other), now),
            )
            connection.execute(
                "INSERT INTO config_entries VALUES ('legacy-other-version', 'document', 'root', ?)",
                (canonical_json(other),),
            )
        connection.execute(
            "INSERT INTO candidates VALUES ('legacy-candidate', 'legacy', NULL, 'VALID', ?, ?, ?)",
            ("0" * 64, now, "2099-01-01T00:00:00+00:00"),
        )
        legacy_payload = {
            "documents": [
                {
                    "config_type": "site",
                    "config_id": "legacy",
                    "schema_ref": "site:1:legacy",
                    "file_version": "1.0.0",
                    "content_sha256": semantic_hash(payload),
                    "payload": payload,
                }
            ]
        }
        connection.execute(
            "INSERT INTO idempotency_requests VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "CreateCandidate",
                "legacy-create",
                hashlib.sha256(canonical_json(legacy_payload).encode()).hexdigest(),
                canonical_json({"candidate_id": "legacy-candidate"}),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO candidate_documents VALUES (?, ?, 'site', 'legacy', '1.0.0', ?, ?, ?)",
            (
                "legacy-candidate",
                document_id,
                "site:1:legacy",
                canonical_json(payload),
                semantic_hash(payload),
            ),
        )
        connection.commit()


def test_v1_migration_preserves_history_and_writes_real_yaml_hash(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_v1_database(path)
    repository = SQLiteRepository(path, application_version="test")
    repository.initialize()
    try:
        backup = path.with_name(path.name + ".pre-v2-backup.sqlite3")
        assert backup.is_file()
        with repository.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert (
                connection.execute(
                    "SELECT migration_id FROM schema_migrations WHERE from_version = 1"
                ).fetchone()[0]
                == MIGRATION_ID
            )
        files = repository.get_revision_files(1)
        assert len(files) == 1
        item = files[0]
        assert item["relative_path"] == "site_legacy_v1.0.0.yaml"
        assert hashlib.sha256(item["yaml_bytes"]).hexdigest() == item["yaml_sha256"]
        with repository.read() as connection:
            candidate = connection.execute(
                "SELECT relative_path, yaml_bytes, yaml_sha256 FROM candidate_documents"
            ).fetchone()
            assert candidate[0] == item["relative_path"]
            assert hashlib.sha256(candidate[1]).hexdigest() == candidate[2]
        payload = {
            "schema_version": 1,
            "file_version": "1.0.0",
            "site_id": "legacy",
            "name": "legacy-name",
        }
        replay = repository.create_candidate(
            actor_id="legacy",
            request_id="legacy-create",
            expected_revision=0,
            documents=(
                PreparedDocument(
                    identity=ConfigIdentity("site", "legacy"),
                    schema_ref="site:1:legacy",
                    file_version="1.0.0",
                    content_sha256=semantic_hash(payload),
                    canonical_payload=payload,
                    relative_path="instances/legacy.yaml",
                    yaml_bytes=b"legacy-yaml",
                    yaml_sha256=hashlib.sha256(b"legacy-yaml").hexdigest(),
                ),
            ),
            ttl_seconds=3600,
            max_candidates=32,
        )
        assert replay == "legacy-candidate"
        restored = repository.get_version(
            ConfigIdentity("site", "legacy"),
            version_id=next(iter(repository.get_revision_members(1).values())),
        )
        assert restored["relative_path"] == item["relative_path"]
        other = repository.get_version(
            ConfigIdentity("site", "legacy"), version_id="legacy-other-version"
        )
        assert other["file_version"] == "2.0.0"
        assert other["relative_path"] is None
        application = ConfigurationApplication(
            repository,
            limits=RepositoryLimits(
                max_request_bytes=4 * 1024 * 1024,
                max_bundle_bytes=8 * 1024 * 1024,
                max_dependencies=1000,
                max_documents_per_publication=100,
                candidate_ttl_seconds=3600,
                max_candidates=32,
                max_backup_jobs=2,
            ),
            backups=SQLiteBackupManager(repository, tmp_path / "backups"),
            own_service_id="configuration-test",
            codec=YamlDocumentCodec(),
        )
        restored_other = application.get_config(
            ConfigIdentity("site", "legacy"), version_id="legacy-other-version"
        )
        assert restored_other.file_version == "2.0.0"
        assert restored_other.file_name == "site_legacy_v2.0.0.yaml"
    finally:
        repository.close()


def test_unmigratable_v1_rolls_back_and_preserves_backup(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    _create_v1_database(path, corrupt=True)
    repository = SQLiteRepository(path, application_version="test")
    with pytest.raises(RuntimeError, match="canonical document root"):
        repository.initialize()
    repository.close()
    assert path.with_name(path.name + ".pre-v2-backup.sqlite3").is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'revision_files'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'candidate_documents'"
            ).fetchone()
            is not None
        )
