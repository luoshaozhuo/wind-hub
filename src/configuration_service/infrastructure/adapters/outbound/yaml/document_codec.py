"""Adapter implementing the application's deterministic document codec port."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from configuration_service.domain.models import (
    ConfigIdentity,
    PreparedDocument,
    RowProjection,
)

from .codec import (
    canonical_json,
    emit_yaml,
    parse_yaml,
    prepare_document,
    reconstruct_document,
    semantic_hash,
)
from .projection import create_projections


class YamlDocumentCodec:
    """Keep YAML libraries and relational serialization outside application use cases."""

    def semantic_hash(self, value: object) -> str:
        return semantic_hash(value)

    def canonical_json(self, value: object) -> str:
        return canonical_json(value)

    def parse_yaml(self, payload: bytes, *, max_bytes: int) -> dict[str, Any]:
        return parse_yaml(payload, max_bytes=max_bytes)

    def prepare_document(
        self,
        identity: ConfigIdentity,
        yaml_bytes: bytes,
        *,
        schema_ref: str,
        schema_version: int,
        max_bytes: int,
    ) -> PreparedDocument:
        return prepare_document(
            identity,
            yaml_bytes,
            schema_ref=schema_ref,
            schema_version=schema_version,
            max_bytes=max_bytes,
        )

    def create_projections(
        self,
        document: PreparedDocument,
        *,
        dependency_versions: Mapping[str, str],
        device_bindings: Mapping[str, tuple[str, str, str, str]],
        table_device_type: str | None,
        point_table_parent: tuple[str, str] | None = None,
    ) -> tuple[RowProjection, ...]:
        return create_projections(
            document,
            dependency_versions=dependency_versions,
            device_bindings=device_bindings,
            table_device_type=table_device_type,
            point_table_parent=point_table_parent,
        )

    def reconstruct_document(
        self,
        projection_rows: tuple[dict[str, Any], ...],
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        return reconstruct_document(projection_rows, expected_sha256=expected_sha256)

    def emit_yaml(self, document: Mapping[str, Any], header_definition: Mapping[str, Any]) -> bytes:
        return emit_yaml(document, header_definition)
