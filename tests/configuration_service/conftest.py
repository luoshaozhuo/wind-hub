"""Configuration Service component fixtures backed by real temporary SQLite files."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import (
    ConfigurationApplication,
    DocumentInput,
    RepositoryLimits,
)
from configuration_service.domain.models import ConfigIdentity
from configuration_service.infrastructure.adapters.outbound.sqlite.backup import (
    SQLiteBackupManager,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from configuration_service.infrastructure.adapters.outbound.yaml.document_codec import (
    YamlDocumentCodec,
)


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[SQLiteRepository]:
    result = SQLiteRepository(tmp_path / "repository.sqlite3", application_version="test")
    result.initialize()
    try:
        yield result
    finally:
        result.close()


@pytest.fixture
def backups(repository: SQLiteRepository, tmp_path: Path) -> SQLiteBackupManager:
    return SQLiteBackupManager(repository, tmp_path / "backups")


@pytest.fixture
def app(repository: SQLiteRepository, backups: SQLiteBackupManager) -> ConfigurationApplication:
    result = ConfigurationApplication(
        repository,
        limits=RepositoryLimits(
            max_request_bytes=4 * 1024 * 1024,
            max_bundle_bytes=8 * 1024 * 1024,
            max_dependencies=1000,
            max_documents_per_publication=100,
            candidate_ttl_seconds=3600,
            max_candidates=32,
            max_backup_jobs=2,
            page_size=100,
        ),
        backups=backups,
        own_service_id="configuration-test",
        codec=YamlDocumentCodec(),
    )
    for specification in iter_schemas():
        result.register_schema(
            config_type=specification.config_type,
            document_schema_version=specification.schema_version,
            fields=specification.fields,
            header=specification.header,
        )
    return result


# Full-directory publication (v1_16) binds every document to an immutable
# repository relative_path; synthetic component documents use the same path
# rules as resolve_publication_identity().
_TEMPLATE_PATHS = {
    "point_table": ("point_tables", "point_table_"),
    "device_model": ("device_models", "device_model_"),
    "protocol": ("protocols", "protocol_"),
    "sink_template": ("sinks", "sink_"),
    "delivery_template": ("deliveries", "delivery_"),
}
_INSTANCE_PATHS = {
    "site": "site",
    "device": "device",
    "collector_tasks": "collector_tasks",
    "scenario": "scenarios",
    "sink": "sinks",
    "delivery": "deliveries",
}


def publication_path(config_type: str, config_id: str, file_version: str) -> str:
    """Resolve only supported immutable publication paths."""
    if config_type in _TEMPLATE_PATHS:
        directory, prefix = _TEMPLATE_PATHS[config_type]
        return f"templates/{directory}/{prefix}{config_id}_v{file_version}.yaml"
    if config_type in _INSTANCE_PATHS:
        return f"instances/{_INSTANCE_PATHS[config_type]}_{config_id}_v{file_version}.yaml"
    if config_type == "service":
        if config_id.startswith("configuration"):
            raise ValueError("configuration bootstrap must stay outside the repository")
        return f"services/service_{config_id}_v{file_version}.yaml"
    if config_type == "deployment":
        return f"deployments/deployment_{config_id}_v{file_version}.yaml"
    if config_type in {"units", "business_points"}:
        if config_id != "standard":
            raise ValueError(f"{config_type} requires the standard catalog identity")
        return f"templates/{config_type}_standard_v{file_version}.yaml"
    if config_type == "observability":
        return f"templates/observability_v{file_version}.yaml"
    if config_type == "semantic_points":
        raise ValueError("semantic_points was retired")
    if config_type == "task":
        raise ValueError("task templates were retired; use collector_tasks Instances")
    raise ValueError(f"unsupported synthetic config_type: {config_type}")


def document_input(
    config_type: str,
    config_id: str,
    file_version: str,
    **fields: Any,
) -> DocumentInput:
    # Sink/Delivery share one repository identity type for their Instance
    # and Template documents. The template_id field disambiguates the YAML
    # path; the stored config_type remains the actual repository type.
    template_family = config_type in {"sink", "delivery"} and "template_id" in fields
    if template_family:
        relative_path = publication_path(f"{config_type}_template", config_id, file_version)
    else:
        relative_path = publication_path(config_type, config_id, file_version)
    payload = {
        "schema_version": 1,
        "file_version": file_version,
        **fields,
    }
    if config_type in _TEMPLATE_PATHS or template_family:
        payload.setdefault("template_id", config_id)
    if config_type == "service":
        payload.setdefault("service_id", config_id)
    if config_type == "deployment":
        payload.setdefault("deployment_id", config_id)
    if config_type == "site":
        payload.setdefault("site_id", config_id)
        payload.setdefault("name", config_id)
    if config_type == "collector_tasks":
        payload.setdefault("tasks", {})
    data = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode()
    return DocumentInput(
        identity=ConfigIdentity(config_type, config_id),
        file_name=relative_path.rsplit("/", 1)[-1],
        yaml_bytes=data,
        relative_path=relative_path,
    )


def publish_documents(
    app: ConfigurationApplication,
    documents: tuple[DocumentInput, ...],
    *,
    expected_revision: int = 0,
    actor_id: str = "publisher",
    request_id: str,
) -> tuple[int, tuple[str, ...]]:
    candidate_id, _ = app.create_candidate(
        actor_id=actor_id,
        request_id=f"{request_id}-candidate",
        expected_revision=expected_revision,
        documents=documents,
        validate_only=False,
    )
    committed = app.publish_candidate(
        actor_id=actor_id,
        request_id=f"{request_id}-publish",
        candidate_id=candidate_id,
        expected_revision=expected_revision,
    )
    return committed.revision_id, committed.version_ids
