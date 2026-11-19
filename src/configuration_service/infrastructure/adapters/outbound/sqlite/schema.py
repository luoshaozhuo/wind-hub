"""Verified, fail-closed SQLite schema migration for Configuration Service."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from configuration_service.infrastructure.adapters.outbound.sqlite.migration_v1 import (
    DDL_V1,
    IMMUTABLE_TABLES_V1,
)

SCHEMA_VERSION = 2
MIGRATION_ID = "full_directory_snapshot_v2"
INITIAL_MIGRATION_ID = "initial_configuration_repository"


def _replace_table(ddl: str, table_name: str, replacement: str) -> str:
    """Replace the single exact DDL declaration, never alter the v1 archive."""
    prefix = f"CREATE TABLE {table_name} ("
    start = ddl.index(prefix)
    end = ddl.index("\n);", start) + len("\n);")
    return ddl[:start] + replacement + ddl[end:]


_NEW_CANDIDATE = """CREATE TABLE candidate_documents (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    relative_path TEXT NOT NULL CHECK (length(relative_path) > 0),
    document_id TEXT NOT NULL,
    config_type TEXT NOT NULL CHECK (length(config_type) > 0),
    config_id TEXT NOT NULL CHECK (length(config_id) > 0),
    file_version TEXT NOT NULL,
    schema_ref TEXT NOT NULL REFERENCES config_schemas(schema_ref),
    canonical_payload_json TEXT NOT NULL CHECK (json_valid(canonical_payload_json)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    yaml_sha256 TEXT NOT NULL CHECK (length(yaml_sha256) = 64),
    yaml_bytes BLOB NOT NULL CHECK (length(yaml_bytes) > 0),
    PRIMARY KEY (candidate_id, relative_path),
    UNIQUE (candidate_id, document_id, file_version)
);"""

_DIRECTORY_TABLES = """CREATE TABLE revision_files (
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    relative_path TEXT NOT NULL CHECK (length(relative_path) > 0),
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    yaml_sha256 TEXT NOT NULL CHECK (length(yaml_sha256) = 64),
    yaml_bytes BLOB NOT NULL CHECK (length(yaml_bytes) > 0),
    PRIMARY KEY (revision_id, relative_path),
    FOREIGN KEY (document_id, version_id)
        REFERENCES config_versions(document_id, version_id)
);
CREATE TABLE revision_manifests (
    revision_id INTEGER PRIMARY KEY REFERENCES revisions(revision_id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    total_bytes INTEGER NOT NULL CHECK (total_bytes > 0),
    file_count INTEGER NOT NULL CHECK (file_count > 0)
);
"""

DDL = _replace_table(DDL_V1, "candidate_documents", _NEW_CANDIDATE)
DDL = DDL.replace("CREATE TABLE config_references (", _DIRECTORY_TABLES + "CREATE TABLE config_references (")
IMMUTABLE_TABLES = IMMUTABLE_TABLES_V1 + ("revision_files", "revision_manifests")


def _immutable_trigger_sql(table: str, operation: str) -> str:
    return (
        f"CREATE TRIGGER immutable_{table}_{operation.lower()} "
        f"BEFORE {operation} ON {table} BEGIN "
        "SELECT RAISE(ABORT, 'immutable configuration record'); END"
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _schema_signature(ddl: str = DDL) -> str:
    return hashlib.sha256(ddl.encode("utf-8")).hexdigest()


def _check_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("SQLite foreign key check failed")


def _verify_schema(connection: sqlite3.Connection, *, legacy: bool = False) -> None:
    version = 1 if legacy else SCHEMA_VERSION
    migration_id = INITIAL_MIGRATION_ID if legacy else MIGRATION_ID
    ddl = DDL_V1 if legacy else DDL
    immutable = IMMUTABLE_TABLES_V1 if legacy else IMMUTABLE_TABLES
    meta = connection.execute(
        "SELECT schema_version, migration_id FROM db_meta WHERE id = 1"
    ).fetchone()
    if meta is None or tuple(meta) != (version, migration_id):
        raise RuntimeError("unsupported database schema version")
    if connection.execute("PRAGMA user_version").fetchone()[0] != version:
        raise RuntimeError("SQLite user_version differs from db_meta")
    migration = connection.execute(
        "SELECT from_version, to_version, checksum FROM schema_migrations "
        "WHERE migration_id = ?", (migration_id,),
    ).fetchone()
    if migration is None or tuple(migration) != (
        version - 1, version, _schema_signature(ddl)
    ):
        raise RuntimeError("database schema migration record mismatch")
    required = {
        statement.split()[2]
        for statement in ddl.split(";")
        if statement.strip().startswith(("CREATE TABLE ", "CREATE INDEX "))
    }
    existing = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )
    }
    if not required.issubset(existing):
        raise RuntimeError("database schema has missing tables or indexes")
    for statement in ddl.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        parts = statement.split()
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (parts[1].lower(), parts[2]),
        ).fetchone()
        if row is None or row[0] != statement:
            raise RuntimeError(f"database schema drift: {parts[2]}")
    for table in immutable:
        for operation in ("UPDATE", "DELETE"):
            name = f"immutable_{table}_{operation.lower()}"
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (name,),
            ).fetchone()
            if row is None or row[0] != _immutable_trigger_sql(table, operation):
                raise RuntimeError(f"database immutability trigger drift: {name}")
    _check_integrity(connection)


def _create_v1(connection: sqlite3.Connection, application_version: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in DDL_V1.split(";"):
            if statement.strip():
                connection.execute(statement)
        for table in IMMUTABLE_TABLES_V1:
            for operation in ("UPDATE", "DELETE"):
                connection.execute(_immutable_trigger_sql(table, operation))
        now = _utc_now()
        connection.execute(
            "INSERT INTO db_meta VALUES (1, 1, ?, ?, ?)",
            (INITIAL_MIGRATION_ID, now, application_version),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 0, 1, ?, ?)",
            (INITIAL_MIGRATION_ID, now, _schema_signature(DDL_V1)),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _migrate_v1(connection: sqlite3.Connection) -> None:
    """Migrate only history-free v1 databases, never invent old source YAML paths."""
    for table in ("revisions", "config_versions", "candidates"):
        count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count:
            raise RuntimeError(
                "legacy repository contains historical versions, revisions or candidates; "
                "verified offline backup and explicit migration are required"
            )
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE candidate_documents")
        names = ("candidate_documents", "revision_files", "revision_manifests")
        for statement in DDL.split(";"):
            statement = statement.strip()
            if any(statement.startswith(f"CREATE TABLE {name} (") for name in names):
                connection.execute(statement)
        for table in ("revision_files", "revision_manifests"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(_immutable_trigger_sql(table, operation))
        now = _utc_now()
        connection.execute(
            "UPDATE db_meta SET schema_version = ?, migration_id = ?, migrated_at = ? "
            "WHERE id = 1",
            (SCHEMA_VERSION, MIGRATION_ID, now),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 1, ?, ?, ?)",
            (MIGRATION_ID, SCHEMA_VERSION, now, _schema_signature()),
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _verify_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def initialize_database(connection: sqlite3.Connection, application_version: str) -> None:
    """Initialize or migrate under the caller's single-owner SQLite lock."""
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if existing:
        if version == SCHEMA_VERSION:
            _verify_schema(connection)
            return
        if version != 1:
            raise RuntimeError(f"unsupported SQLite database schema version: {version}")
        _verify_schema(connection, legacy=True)
    else:
        if version != 0:
            raise RuntimeError("nonempty schema version without database tables")
        _create_v1(connection, application_version)
        _verify_schema(connection, legacy=True)
    _migrate_v1(connection)
