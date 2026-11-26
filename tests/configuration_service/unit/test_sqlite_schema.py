"""Verify atomic schema evolution and refusal to invent lost legacy directory paths."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from configuration_service.infrastructure.adapters.outbound.sqlite.initial_schema import (
    DDL_V1,
    IMMUTABLE_TABLES_V1,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.schema import (
    _immutable_trigger_sql,
)


def test_fresh_repository_initializes_atomic_directory_schema(tmp_path: Path) -> None:
    path = tmp_path / "configuration.sqlite3"
    repository = SQLiteRepository(path, application_version="test")
    repository.initialize()
    try:
        with repository.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"revision_files", "revision_manifests", "candidate_documents"} <= tables
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(candidate_documents)")
            }
            assert {"relative_path", "yaml_sha256", "yaml_bytes"} <= columns
    finally:
        repository.close()


def test_empty_history_v1_database_migrates_atomically_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "empty-v1.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in DDL_V1.split(";"):
            if statement.strip():
                connection.execute(statement)
        for table in IMMUTABLE_TABLES_V1:
            for operation in ("UPDATE", "DELETE"):
                connection.execute(_immutable_trigger_sql(table, operation))
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO db_meta VALUES (1, 1, ?, ?, ?)",
            ("initial_configuration_repository", now, "test"),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 0, 1, ?, ?)",
            (
                "initial_configuration_repository",
                now,
                hashlib.sha256(DDL_V1.encode("utf-8")).hexdigest(),
            ),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    repository = SQLiteRepository(path, application_version="test")
    repository.initialize()
    try:
        with repository.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            # v2 directory publication tables replace the v1 candidate layout.
            assert {"revision_files", "revision_manifests", "candidate_documents"} <= tables
            assert "semantic_points" not in tables
            migrations = [
                tuple(row)
                for row in connection.execute(
                    "SELECT from_version, to_version FROM schema_migrations " "ORDER BY to_version"
                )
            ]
            assert migrations == [(0, 1), (1, 2)]
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # A second initialize() is a no-op under the single-owner lock.
        repository.close()
        again = SQLiteRepository(path, application_version="test")
        again.initialize()
        with again.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        again.close()
    finally:
        repository.close()


def test_populated_legacy_database_fails_closed_without_rewriting_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in DDL_V1.split(";"):
            if statement.strip():
                connection.execute(statement)
        for table in IMMUTABLE_TABLES_V1:
            for operation in ("UPDATE", "DELETE"):
                connection.execute(_immutable_trigger_sql(table, operation))
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO db_meta VALUES (1, 1, ?, ?, ?)",
            ("initial_configuration_repository", now, "test"),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 0, 1, ?, ?)",
            (
                "initial_configuration_repository",
                now,
                hashlib.sha256(DDL_V1.encode("utf-8")).hexdigest(),
            ),
        )
        connection.execute(
            "INSERT INTO revisions (parent_revision_id, publisher, request_id, created_at) "
            "VALUES (NULL, 'operator', 'legacy-1', ?)",
            (now,),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    repository = SQLiteRepository(path, application_version="test")
    with pytest.raises(
        RuntimeError,
        match="legacy repository contains historical versions, revisions or candidates",
    ):
        repository.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'revision_files'"
            ).fetchone()
            is None
        )
