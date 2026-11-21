"""Frozen initial Configuration SQLite schema for lossless migration validation."""

DDL_V1 = """
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

IMMUTABLE_TABLES_V1 = (
    "schema_migrations",
    "config_schemas",
    "config_documents",
    "config_versions",
    "revisions",
    "revision_members",
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
