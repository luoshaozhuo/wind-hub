"""SQLite schema and atomic database migration for Configuration Service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from configuration_service.infrastructure.adapters.outbound.yaml.codec import (
    emit_yaml,
    reconstruct_document,
)

SCHEMA_VERSION = 2
INITIAL_MIGRATION_ID = "initial_configuration_repository"
MIGRATION_ID = "configuration_revision_files"
V1_SCHEMA_VERSION = 1

V1_DDL = """
CREATE TABLE db_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    migration_id TEXT NOT NULL,
    migrated_at TEXT NOT NULL,
    application_version TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    CHECK (to_version = from_version + 1)
);
CREATE TABLE config_schemas (
    schema_ref TEXT PRIMARY KEY,
    config_type TEXT NOT NULL CHECK (length(config_type) > 0),
    document_schema_version INTEGER NOT NULL CHECK (document_schema_version > 0),
    schema_json TEXT NOT NULL CHECK (json_valid(schema_json)),
    header_definition_json TEXT NOT NULL CHECK (json_valid(header_definition_json)),
    definition_sha256 TEXT NOT NULL CHECK (length(definition_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (config_type, document_schema_version)
);
CREATE TABLE config_documents (
    document_id TEXT PRIMARY KEY,
    config_type TEXT NOT NULL CHECK (length(config_type) > 0),
    config_id TEXT NOT NULL CHECK (length(config_id) > 0),
    UNIQUE (config_type, config_id)
);
CREATE TABLE config_versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES config_documents(document_id),
    schema_ref TEXT NOT NULL REFERENCES config_schemas(schema_ref),
    file_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (document_id, file_version),
    UNIQUE (document_id, version_id)
);
CREATE TABLE revisions (
    revision_id INTEGER PRIMARY KEY,
    parent_revision_id INTEGER REFERENCES revisions(revision_id),
    publisher TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE revision_members (
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    document_id TEXT NOT NULL REFERENCES config_documents(document_id),
    version_id TEXT NOT NULL,
    PRIMARY KEY (revision_id, document_id),
    FOREIGN KEY (document_id, version_id)
        REFERENCES config_versions(document_id, version_id)
);
CREATE TABLE config_references (
    source_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    target_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    relation TEXT NOT NULL CHECK (length(relation) > 0),
    PRIMARY KEY (source_version_id, target_version_id, relation)
);
CREATE INDEX ix_config_references_target ON config_references(target_version_id);
CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    base_revision_id INTEGER REFERENCES revisions(revision_id),
    status TEXT NOT NULL CHECK (status IN ('VALID', 'REJECTED', 'PUBLISHED', 'EXPIRED')),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE candidate_documents (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    document_id TEXT NOT NULL,
    config_type TEXT NOT NULL CHECK (length(config_type) > 0),
    config_id TEXT NOT NULL CHECK (length(config_id) > 0),
    file_version TEXT NOT NULL,
    schema_ref TEXT NOT NULL REFERENCES config_schemas(schema_ref),
    canonical_payload_json TEXT NOT NULL CHECK (json_valid(canonical_payload_json)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    PRIMARY KEY (candidate_id, document_id)
);
CREATE TABLE idempotency_requests (
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    completed_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, operation, request_id)
);
CREATE TABLE version_events (
    event_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    event_type TEXT NOT NULL CHECK (event_type = 'DEPRECATED'),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (version_id, event_type)
);
CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_id TEXT NOT NULL,
    revision_id INTEGER REFERENCES revisions(revision_id),
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json))
);
CREATE TABLE backup_tasks (
    task_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'INTERRUPTED')
    ),
    path TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    database_schema_version INTEGER,
    integrity_verified INTEGER NOT NULL DEFAULT 0
        CHECK (integrity_verified IN (0, 1)),
    UNIQUE (actor_id, request_id)
);
CREATE TABLE unit_definitions (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    unit_id TEXT NOT NULL,
    quantity_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    scale_to_base TEXT NOT NULL,
    offset_to_base TEXT NOT NULL,
    base_unit_id TEXT,
    PRIMARY KEY (config_version_id, unit_id),
    FOREIGN KEY (config_version_id, base_unit_id)
        REFERENCES unit_definitions(config_version_id, unit_id)
);
CREATE TABLE semantic_points (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    semantic_point_id TEXT NOT NULL,
    data_type TEXT NOT NULL,
    standard_unit_version_id TEXT NOT NULL,
    standard_unit_id TEXT NOT NULL,
    description TEXT,
    constraints_json TEXT NOT NULL CHECK (json_valid(constraints_json)),
    PRIMARY KEY (config_version_id, semantic_point_id),
    FOREIGN KEY (standard_unit_version_id, standard_unit_id)
        REFERENCES unit_definitions(config_version_id, unit_id)
);
CREATE TABLE business_points (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    device_type TEXT NOT NULL,
    business_point_id TEXT NOT NULL,
    semantic_config_version_id TEXT NOT NULL,
    semantic_point_id TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (config_version_id, device_type, business_point_id),
    FOREIGN KEY (semantic_config_version_id, semantic_point_id)
        REFERENCES semantic_points(config_version_id, semantic_point_id)
);
CREATE TABLE point_tables (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    point_table_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    protocol TEXT NOT NULL,
    parent_version_id TEXT,
    parent_point_table_id TEXT,
    PRIMARY KEY (config_version_id, point_table_id),
    FOREIGN KEY (parent_version_id, parent_point_table_id)
        REFERENCES point_tables(config_version_id, point_table_id),
    CHECK ((parent_version_id IS NULL) = (parent_point_table_id IS NULL))
);
CREATE TABLE point_mappings (
    config_version_id TEXT NOT NULL,
    point_table_id TEXT NOT NULL,
    point_id TEXT NOT NULL,
    business_config_version_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    business_point_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_unit_version_id TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    scale TEXT NOT NULL,
    offset TEXT NOT NULL,
    address_json TEXT NOT NULL CHECK (json_valid(address_json)),
    point_groups_json TEXT NOT NULL CHECK (json_valid(point_groups_json)),
    PRIMARY KEY (config_version_id, point_table_id, point_id),
    FOREIGN KEY (config_version_id, point_table_id)
        REFERENCES point_tables(config_version_id, point_table_id),
    FOREIGN KEY (business_config_version_id, device_type, business_point_id)
        REFERENCES business_points(config_version_id, device_type, business_point_id),
    FOREIGN KEY (source_unit_version_id, source_unit_id)
        REFERENCES unit_definitions(config_version_id, unit_id)
);
CREATE TABLE device_models (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    model_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    properties_json TEXT NOT NULL CHECK (json_valid(properties_json)),
    PRIMARY KEY (config_version_id, model_id)
);
CREATE TABLE devices (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    device_id TEXT NOT NULL,
    model_version_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    point_table_version_id TEXT NOT NULL,
    point_table_id TEXT NOT NULL,
    properties_json TEXT NOT NULL CHECK (json_valid(properties_json)),
    PRIMARY KEY (config_version_id, device_id),
    FOREIGN KEY (model_version_id, model_id)
        REFERENCES device_models(config_version_id, model_id),
    FOREIGN KEY (point_table_version_id, point_table_id)
        REFERENCES point_tables(config_version_id, point_table_id)
);
CREATE TABLE app_service_configs (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    service_id TEXT NOT NULL,
    module_type TEXT NOT NULL,
    params_json TEXT NOT NULL CHECK (json_valid(params_json)),
    PRIMARY KEY (config_version_id, service_id)
);
CREATE TABLE config_entries (
    config_version_id TEXT NOT NULL REFERENCES config_versions(version_id),
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    properties_json TEXT NOT NULL CHECK (json_valid(properties_json)),
    PRIMARY KEY (config_version_id, object_type, object_id)
);
"""

_V1_CANDIDATE_DOCUMENTS_DDL = """CREATE TABLE candidate_documents (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    document_id TEXT NOT NULL,
    config_type TEXT NOT NULL CHECK (length(config_type) > 0),
    config_id TEXT NOT NULL CHECK (length(config_id) > 0),
    file_version TEXT NOT NULL,
    schema_ref TEXT NOT NULL REFERENCES config_schemas(schema_ref),
    canonical_payload_json TEXT NOT NULL CHECK (json_valid(canonical_payload_json)),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    PRIMARY KEY (candidate_id, document_id)
)"""

_CANDIDATE_DOCUMENTS_DDL = """CREATE TABLE candidate_documents (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    relative_path TEXT NOT NULL CHECK (length(relative_path) > 0),
    document_id TEXT NOT NULL,
    config_type TEXT NOT NULL CHECK (length(config_type) > 0),
    config_id TEXT NOT NULL CHECK (length(config_id) > 0),
    file_version TEXT NOT NULL,
    schema_ref TEXT NOT NULL REFERENCES config_schemas(schema_ref),
    canonical_payload_json TEXT NOT NULL CHECK (json_valid(canonical_payload_json)),
    yaml_bytes BLOB NOT NULL,
    yaml_sha256 TEXT NOT NULL CHECK (length(yaml_sha256) = 64),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    PRIMARY KEY (candidate_id, relative_path)
)"""

_REVISION_FILES_DDL = """CREATE TABLE revision_files (
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    relative_path TEXT NOT NULL CHECK (length(relative_path) > 0),
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    yaml_sha256 TEXT NOT NULL CHECK (length(yaml_sha256) = 64),
    yaml_bytes BLOB NOT NULL,
    PRIMARY KEY (revision_id, relative_path),
    FOREIGN KEY (document_id, version_id)
        REFERENCES config_versions(document_id, version_id)
);
CREATE INDEX ix_revision_files_version ON revision_files(version_id, revision_id DESC);
"""

DDL = V1_DDL.replace(
    _V1_CANDIDATE_DOCUMENTS_DDL,
    _CANDIDATE_DOCUMENTS_DDL,
).replace(
    "CREATE TABLE config_references",
    _REVISION_FILES_DDL + "CREATE TABLE config_references",
    1,
)
if DDL == V1_DDL or _CANDIDATE_DOCUMENTS_DDL not in DDL or _REVISION_FILES_DDL not in DDL:
    raise RuntimeError("failed to derive Configuration Schema v2")

IMMUTABLE_TABLES = (
    "schema_migrations",
    "config_schemas",
    "config_documents",
    "config_versions",
    "revisions",
    "revision_members",
    "revision_files",
    "config_references",
    "unit_definitions",
    "semantic_points",
    "business_points",
    "point_tables",
    "point_mappings",
    "device_models",
    "devices",
    "app_service_configs",
    "config_entries",
    "version_events",
    "audit_events",
    "idempotency_requests",
)
LEGACY_IMMUTABLE_TABLES = tuple(name for name in IMMUTABLE_TABLES if name != "revision_files")


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


def _verify_ddl_schema(
    connection: sqlite3.Connection, ddl: str, immutable_tables: tuple[str, ...]
) -> None:
    required = set(_table_names(ddl))
    actual = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not required.issubset(actual):
        raise RuntimeError("database schema has missing tables")
    for statement in ddl.split(";"):
        statement = statement.strip()
        if not statement:
            continue
        tokens = statement.split()
        object_kind = tokens[1].lower()
        object_name = tokens[2]
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_kind, object_name),
        ).fetchone()
        if row is None or row[0] != statement:
            raise RuntimeError(f"database schema drift: {object_name}")
    for table in immutable_tables:
        for operation in ("UPDATE", "DELETE"):
            name = f"immutable_{table}_{operation.lower()}"
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (name,),
            ).fetchone()
            if row is None or row[0] != _immutable_trigger_sql(table, operation):
                raise RuntimeError(f"database immutability trigger drift: {name}")
    _check_integrity(connection)


def _verify_migration_row(
    connection: sqlite3.Connection,
    migration_id: str,
    from_version: int,
    to_version: int,
    checksum: str,
) -> None:
    row = connection.execute(
        "SELECT from_version, to_version, checksum FROM schema_migrations "
        "WHERE migration_id = ?",
        (migration_id,),
    ).fetchone()
    if row is None or tuple(row) != (from_version, to_version, checksum):
        raise RuntimeError("database schema migration record mismatch")


def _verify_legacy_schema(connection: sqlite3.Connection) -> None:
    meta = connection.execute(
        "SELECT schema_version, migration_id FROM db_meta WHERE id = 1"
    ).fetchone()
    if meta is None or tuple(meta) != (V1_SCHEMA_VERSION, INITIAL_MIGRATION_ID):
        raise RuntimeError("unsupported legacy database schema")
    if connection.execute("PRAGMA user_version").fetchone()[0] != V1_SCHEMA_VERSION:
        raise RuntimeError("legacy SQLite user_version differs from db_meta")
    _verify_migration_row(
        connection,
        INITIAL_MIGRATION_ID,
        0,
        V1_SCHEMA_VERSION,
        _schema_signature(V1_DDL),
    )
    _verify_ddl_schema(connection, V1_DDL, LEGACY_IMMUTABLE_TABLES)


def _verify_schema(connection: sqlite3.Connection) -> None:
    meta = connection.execute(
        "SELECT schema_version, migration_id FROM db_meta WHERE id = 1"
    ).fetchone()
    if meta is None or tuple(meta) != (SCHEMA_VERSION, MIGRATION_ID):
        raise RuntimeError("unsupported database schema version")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("SQLite user_version differs from db_meta")
    _verify_migration_row(
        connection,
        INITIAL_MIGRATION_ID,
        0,
        V1_SCHEMA_VERSION,
        _schema_signature(V1_DDL),
    )
    _verify_migration_row(
        connection, MIGRATION_ID, V1_SCHEMA_VERSION, SCHEMA_VERSION, _schema_signature()
    )
    _verify_ddl_schema(connection, DDL, IMMUTABLE_TABLES)


def _table_names(ddl: str) -> tuple[str, ...]:
    return tuple(line.split()[2] for line in ddl.splitlines() if line.startswith("CREATE TABLE "))


def _canonical_path(config_type: str, config_id: str, file_version: str) -> str:
    if not config_type or not config_id or not file_version:
        raise RuntimeError("legacy file identity is incomplete")
    return f"{config_type}_{config_id}_v{file_version}.yaml"


def _candidate_yaml(row: sqlite3.Row) -> bytes:
    connection_schema = row["header_definition_json"]
    if not isinstance(connection_schema, str):
        raise RuntimeError("legacy candidate header is missing")
    payload = json.loads(row["canonical_payload_json"])
    if not isinstance(payload, dict):
        raise RuntimeError("legacy candidate payload is not a document")
    return emit_yaml(payload, json.loads(connection_schema))


def _version_yaml(connection: sqlite3.Connection, version_id: str) -> bytes:
    version = connection.execute(
        "SELECT v.content_sha256, s.header_definition_json FROM config_versions AS v "
        "JOIN config_schemas AS s ON s.schema_ref = v.schema_ref WHERE v.version_id = ?",
        (version_id,),
    ).fetchone()
    if version is None:
        raise RuntimeError("legacy version has no registered schema")
    rows = connection.execute(
        "SELECT object_type, object_id, properties_json FROM config_entries "
        "WHERE config_version_id = ? ORDER BY rowid",
        (version_id,),
    ).fetchall()
    payload = reconstruct_document(
        tuple(dict(row) for row in rows),
        expected_sha256=str(version["content_sha256"]),
    )
    return emit_yaml(payload, json.loads(str(version["header_definition_json"])))


def _migrate_v1_to_v2(
    connection: sqlite3.Connection, application_version: str, backup_path: Path | None
) -> None:
    if backup_path is None:
        raise RuntimeError("legacy migration requires a verified backup path")
    if backup_path.exists() or backup_path.is_symlink():
        raise RuntimeError("refusing to overwrite an existing pre-migration backup")
    connection.execute("VACUUM INTO ?", (str(backup_path),))
    now = _utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE candidate_documents RENAME TO candidate_documents_v1")
        connection.execute(_CANDIDATE_DOCUMENTS_DDL)
        legacy_candidates = connection.execute(
            "SELECT c.*, s.header_definition_json FROM candidate_documents_v1 AS c "
            "JOIN config_schemas AS s ON s.schema_ref = c.schema_ref"
        ).fetchall()
        for row in legacy_candidates:
            yaml_bytes = _candidate_yaml(row)
            relative_path = _canonical_path(
                str(row["config_type"]), str(row["config_id"]), str(row["file_version"])
            )
            connection.execute(
                "INSERT INTO candidate_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["candidate_id"],
                    relative_path,
                    row["document_id"],
                    row["config_type"],
                    row["config_id"],
                    row["file_version"],
                    row["schema_ref"],
                    row["canonical_payload_json"],
                    yaml_bytes,
                    hashlib.sha256(yaml_bytes).hexdigest(),
                    row["content_sha256"],
                ),
            )
        connection.execute("DROP TABLE candidate_documents_v1")
        for statement in _REVISION_FILES_DDL.split(";"):
            if statement.strip():
                connection.execute(statement)
        members = connection.execute(
            "SELECT m.revision_id, m.document_id, m.version_id, d.config_type, d.config_id, "
            "v.file_version FROM revision_members AS m "
            "JOIN config_documents AS d ON d.document_id = m.document_id "
            "JOIN config_versions AS v ON v.version_id = m.version_id"
        ).fetchall()
        for row in members:
            yaml_bytes = _version_yaml(connection, str(row["version_id"]))
            relative_path = _canonical_path(
                str(row["config_type"]), str(row["config_id"]), str(row["file_version"])
            )
            connection.execute(
                "INSERT INTO revision_files VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["revision_id"],
                    relative_path,
                    row["document_id"],
                    row["version_id"],
                    hashlib.sha256(yaml_bytes).hexdigest(),
                    yaml_bytes,
                ),
            )
        for operation in ("UPDATE", "DELETE"):
            connection.execute(_immutable_trigger_sql("revision_files", operation))
        connection.execute(
            "UPDATE db_meta SET schema_version = ?, migration_id = ?, migrated_at = ?, "
            "application_version = ? WHERE id = 1",
            (SCHEMA_VERSION, MIGRATION_ID, now, application_version),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?, ?)",
            (MIGRATION_ID, V1_SCHEMA_VERSION, SCHEMA_VERSION, now, _schema_signature()),
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _verify_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def initialize_database(
    connection: sqlite3.Connection, application_version: str, backup_path: Path | None = None
) -> None:
    """Initialize v2, or atomically migrate a verified v1 database with a backup."""
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if existing:
        if version == V1_SCHEMA_VERSION:
            _verify_legacy_schema(connection)
            _migrate_v1_to_v2(connection, application_version, backup_path)
            return
        if version == SCHEMA_VERSION:
            _verify_schema(connection)
            return
        raise RuntimeError(f"unsupported SQLite database schema version: {version}")
    if version != 0:
        raise RuntimeError("nonempty schema version without database tables")

    connection.execute("BEGIN IMMEDIATE")
    try:
        for sql in DDL.split(";"):
            if sql.strip():
                connection.execute(sql)
        for name in IMMUTABLE_TABLES:
            for operation in ("UPDATE", "DELETE"):
                connection.execute(_immutable_trigger_sql(name, operation))
        now = _utc_now()
        connection.execute(
            "INSERT INTO db_meta VALUES (1, ?, ?, ?, ?)",
            (SCHEMA_VERSION, MIGRATION_ID, now, application_version),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 0, ?, ?, ?)",
            (INITIAL_MIGRATION_ID, V1_SCHEMA_VERSION, now, _schema_signature(V1_DDL)),
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?, ?)",
            (MIGRATION_ID, V1_SCHEMA_VERSION, SCHEMA_VERSION, now, _schema_signature()),
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    _verify_schema(connection)
