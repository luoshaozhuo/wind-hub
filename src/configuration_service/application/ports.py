"""Application ports for repository persistence and document serialization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from configuration_service.application.directory_manifest import DirectoryManifest
from configuration_service.domain.models import (
    CommitResult,
    ConfigIdentity,
    PreparedDocument,
    PreparedVersion,
    RowProjection,
)


class RepositoryPort(Protocol):
    """Operations required by configuration use cases, independent of SQLite."""

    def register_schema(
        self,
        *,
        schema_ref: str,
        config_type: str,
        document_schema_version: int,
        schema_definition: Mapping[str, Any],
        header_definition: Mapping[str, Any],
    ) -> None: ...

    def get_schema_by_type(
        self, config_type: str, document_schema_version: int
    ) -> dict[str, Any]: ...

    def create_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        expected_revision: int,
        documents: Sequence[PreparedDocument],
        manifest: DirectoryManifest,
        ttl_seconds: int,
        max_candidates: int,
    ) -> str: ...

    def get_candidate(self, candidate_id: str, actor_id: str) -> dict[str, Any]: ...

    def publish_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        candidate_id: str,
        expected_revision: int,
        versions: Sequence[PreparedVersion],
    ) -> CommitResult: ...

    def resolve_version(self, identity: ConfigIdentity, file_version: str) -> str: ...

    def get_revision_members(self, revision_id: int) -> dict[ConfigIdentity, str]: ...

    def get_version_identity(self, version_id: str) -> ConfigIdentity: ...

    def get_document_schema(self, schema_ref: str) -> dict[str, Any]: ...

    def get_projection_rows(self, version_id: str, table: str) -> tuple[dict[str, Any], ...]: ...

    def get_version(
        self,
        identity: ConfigIdentity,
        *,
        version_id: str | None = None,
        revision_id: int | None = None,
    ) -> dict[str, Any]: ...

    def get_bundle_versions(
        self,
        roots: Sequence[ConfigIdentity],
        revision_id: int,
        *,
        max_versions: int,
    ) -> tuple[dict[str, Any], ...]: ...

    def get_point_table_versions(
        self,
        point_table_id: str,
        *,
        max_versions: int,
    ) -> tuple[dict[str, Any], ...]: ...

    def get_version_closure(
        self,
        version_ids: Sequence[str],
        *,
        max_versions: int,
    ) -> tuple[dict[str, Any], ...]: ...

    def create_backup_task(
        self,
        *,
        actor_id: str,
        request_id: str,
        label: str,
        max_active_jobs: int,
    ) -> str: ...

    def current_revision(self) -> int: ...

    def get_current_manifest(self) -> tuple[int, str, str | None]: ...

    def get_version_source(
        self, version_id: str, *, revision_id: int = 0
    ) -> tuple[str, bytes, str] | None: ...

    def get_revision_timestamp(self, revision_id: int) -> str: ...

    def list_configs(
        self,
        *,
        revision_id: int,
        page_size: int,
        after: ConfigIdentity | None = None,
        config_type: str = "",
        config_id_prefix: str = "",
        module_type: str = "",
    ) -> tuple[dict[str, Any], ...]: ...

    def version_snapshot(self, identity: ConfigIdentity) -> tuple[int, int]: ...

    def list_config_versions(
        self,
        identity: ConfigIdentity,
        *,
        watermark: int,
        snapshot_revision: int,
        page_size: int,
        after_rowid: int = 0,
        include_deprecated: bool = False,
    ) -> tuple[dict[str, Any], ...]: ...

    def list_references(
        self,
        *,
        version_id: str,
        inbound: bool,
        page_size: int,
        after: tuple[str, str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]: ...

    def set_recommended_version(
        self,
        *,
        actor_id: str,
        request_id: str,
        identity: ConfigIdentity,
        version_id: str,
        expected_revision: int,
    ) -> tuple[int, str | None]: ...

    def deprecate_version(
        self,
        *,
        actor_id: str,
        request_id: str,
        version_id: str,
        reason: str,
        expected_revision: int,
    ) -> tuple[int, int]: ...

    def get_stats(self, *, config_type: str = "") -> dict[str, int]: ...

    def storage_stats(self) -> dict[str, int | None]: ...

    def get_backup_task(self, task_id: str) -> dict[str, Any]: ...

    def record_rejected_operation(
        self, *, actor_id: str, operation: str, error_code: str
    ) -> None: ...

    def operational_metrics(self) -> dict[str, int]: ...

    def diagnose_storage(self) -> dict[str, tuple[bool, str]]: ...

    def list_audit_events(
        self,
        *,
        after: tuple[str, str] | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        operation_filter: str = "",
        page_size: int = 100,
    ) -> tuple[dict[str, Any], ...]: ...


class BackupPort(Protocol):
    def run(self, task_id: str) -> None: ...


class DocumentCodecPort(Protocol):
    """A deterministic YAML codec and normalized-record projection policy."""

    def semantic_hash(self, value: object) -> str: ...

    def canonical_json(self, value: object) -> str: ...

    def parse_yaml(self, payload: bytes, *, max_bytes: int) -> dict[str, Any]: ...

    def prepare_document(
        self,
        identity: ConfigIdentity,
        yaml_bytes: bytes,
        *,
        schema_ref: str,
        schema_version: int,
        max_bytes: int,
    ) -> PreparedDocument: ...

    def create_projections(
        self,
        document: PreparedDocument,
        *,
        dependency_versions: Mapping[str, str],
        device_bindings: Mapping[str, tuple[str, str, str, str]],
        table_device_type: str | None,
        point_table_parent: tuple[str, str] | None = None,
    ) -> tuple[RowProjection, ...]: ...

    def reconstruct_document(
        self,
        projection_rows: tuple[dict[str, Any], ...],
        *,
        expected_sha256: str,
    ) -> dict[str, Any]: ...

    def emit_yaml(
        self, document: Mapping[str, Any], header_definition: Mapping[str, Any]
    ) -> bytes: ...
