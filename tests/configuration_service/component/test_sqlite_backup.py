"""Real SQLite DDL, constraints, recovery and Online Backup tests."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import (
    IdempotencyConflictError,
    RepositoryCapacityExceededError,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.backup import (
    SQLiteBackupManager,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.schema import (
    DDL,
    IMMUTABLE_TABLES,
    SCHEMA_VERSION,
)
from tests.configuration_service.conftest import document_input, publish_documents

pytestmark = pytest.mark.component


def test_initial_schema_has_24_tables_constraints_and_consistent_metadata(
    repository: SQLiteRepository,
) -> None:
    with repository.read() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert len(tables) == 24
        assert len(DDL.split("CREATE TABLE ")) - 1 == 24
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute("SELECT schema_version FROM db_meta").fetchone()[0] == SCHEMA_VERSION
        )
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT from_version, to_version FROM schema_migrations ORDER BY to_version"
            )
        ] == [(0, 1), (1, SCHEMA_VERSION)]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        triggers = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone()[0]
        assert triggers == len(IMMUTABLE_TABLES) * 2
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        foreign_keys = sum(
            len(connection.execute(f"PRAGMA foreign_key_list({table})").fetchall())
            for table in tables
        )
        assert foreign_keys >= 23


def test_composite_foreign_keys_and_immutable_history_are_enforced(
    repository: SQLiteRepository, app: ConfigurationApplication
) -> None:
    _, (version_id,) = publish_documents(
        app, (document_input("site", "alpha", "1.0.0"),), request_id="constraints"
    )
    with repository.write() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO revision_members VALUES (999, 'missing-document', 'missing-version')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE config_versions SET file_version='9.9.9' WHERE version_id=?",
                (version_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM config_versions WHERE version_id=?", (version_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO config_references VALUES (?, 'does-not-exist', 'bad')",
                (version_id,),
            )


def test_unknown_schema_version_is_rejected(repository: SQLiteRepository, tmp_path: Path) -> None:
    path = tmp_path / "repository.sqlite3"
    repository.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    unknown = SQLiteRepository(path, application_version="test")
    with pytest.raises(RuntimeError, match="unsupported SQLite database schema version"):
        unknown.initialize()


def test_schema_trigger_drift_is_rejected(repository: SQLiteRepository, tmp_path: Path) -> None:
    path = tmp_path / "repository.sqlite3"
    repository.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER immutable_config_versions_update")
    drifted = SQLiteRepository(path, application_version="test")
    with pytest.raises(RuntimeError, match="trigger drift"):
        drifted.initialize()


def test_backup_is_independent_verified_snapshot_with_stable_metadata(
    app: ConfigurationApplication,
    repository: SQLiteRepository,
    backups: SQLiteBackupManager,
) -> None:
    _, (version_id,) = publish_documents(
        app, (document_input("site", "alpha", "1.0.0"),), request_id="backup-source"
    )
    task_id = app.create_backup("admin", "backup-request", "acceptance")
    assert app.create_backup("admin", "backup-request", "acceptance") == task_id
    backups.run(task_id)
    task = repository.get_backup_task(task_id)
    assert task["state"] == "SUCCEEDED"
    assert task["integrity_verified"] == 1
    assert task["database_schema_version"] == SCHEMA_VERSION
    backup_path = Path(task["path"])
    assert backup_path.stat().st_size == task["size_bytes"] > 0
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == task["sha256"]
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM config_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 3
    restored = SQLiteRepository(backup_path, application_version="test")
    restored.initialize()
    try:
        identity = document_input("site", "alpha", "1.0.0").identity
        assert restored.get_version(identity, version_id=version_id)["version_id"] == version_id
        assert restored.current_revision() == 1
        assert len(restored.list_audit_events()) >= 3
    finally:
        restored.close()


def test_interrupted_backup_recovers_as_interrupted_and_cleans_only_part_files(
    app: ConfigurationApplication,
    repository: SQLiteRepository,
    backups: SQLiteBackupManager,
) -> None:
    task_id = app.create_backup("admin", "interrupted", "crash")
    repository.start_backup_task(task_id)
    orphan = backups._backup_root / f".{task_id}.{'a' * 32}.part"
    backups._backup_root.mkdir()
    orphan.write_bytes(b"partial")
    keep = backups._backup_root / "keep.sqlite3"
    keep.write_bytes(b"keep")
    assert backups.recover() == 1
    task = repository.get_backup_task(task_id)
    assert task["state"] == "INTERRUPTED"
    assert task["finished_at"] is not None
    assert not orphan.exists()
    assert keep.read_bytes() == b"keep"


def test_backup_symlink_root_fails_closed_without_success_claim(
    app: ConfigurationApplication,
    repository: SQLiteRepository,
    tmp_path: Path,
) -> None:
    real = tmp_path / "actual-backups"
    real.mkdir()
    link = tmp_path / "linked-backups"
    link.symlink_to(real, target_is_directory=True)
    manager = SQLiteBackupManager(repository, link)
    task_id = app.create_backup("admin", "symlink", "unsafe")
    with pytest.raises(RuntimeError, match="symlink"):
        manager.run(task_id)
    task = repository.get_backup_task(task_id)
    assert task["state"] == "FAILED"
    assert task["sha256"] is None
    assert list(real.iterdir()) == []


def test_backup_queue_limit_and_idempotency_conflict(
    app: ConfigurationApplication, repository: SQLiteRepository
) -> None:
    first = app.create_backup("admin", "one", "label")
    assert repository.get_backup_task(first)["state"] == "QUEUED"
    with pytest.raises(IdempotencyConflictError, match="request_id already used"):
        app.create_backup("admin", "one", "different")
    app.create_backup("admin", "two", "label")
    with pytest.raises(RepositoryCapacityExceededError, match="capacity exhausted"):
        app.create_backup("admin", "three", "label")


def test_online_backup_during_wal_writes_is_consistent(
    app: ConfigurationApplication,
    repository: SQLiteRepository,
    backups: SQLiteBackupManager,
) -> None:
    task_id = app.create_backup("admin", "concurrent-backup", "wal")
    with repository.write() as connection:
        for index in range(3000):
            connection.execute(
                "INSERT INTO audit_events VALUES "
                "(?, 'writer', 'Load', ?, NULL, 'SUCCEEDED', ?, '{}')",
                (f"initial-{index}", f"target-{index}", f"2026-01-01T00:00:{index % 60:02d}+00:00"),
            )
    started = threading.Event()
    original = repository.backup_to

    def delayed_backup(path: Path) -> None:
        started.set()
        original(path)

    repository.backup_to = delayed_backup
    writer_error: list[BaseException] = []

    def write_during_backup() -> None:
        try:
            assert started.wait(5)
            for index in range(100):
                with repository.write() as connection:
                    connection.execute(
                        "INSERT INTO audit_events VALUES "
                        "(?, 'writer', 'Load', ?, NULL, 'SUCCEEDED', ?, '{}')",
                        (f"wal-{index}", "wal-target", f"2026-01-02T00:00:{index % 60:02d}+00:00"),
                    )
        except BaseException as exc:
            writer_error.append(exc)

    writer = threading.Thread(target=write_during_backup)
    writer.start()
    backups.run(task_id)
    writer.join(timeout=30)
    assert not writer.is_alive()
    assert writer_error == []
    task = repository.get_backup_task(task_id)
    assert task["state"] == "SUCCEEDED"
    with sqlite3.connect(task["path"]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        assert 3000 <= count <= 3100
