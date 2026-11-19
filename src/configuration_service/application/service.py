"""Configuration repository use cases independent of protobuf and transport."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from core.configuration.publication_identity import resolve_publication_identity
from configuration_service.application.directory_manifest import (
    DirectoryFile,
    DirectoryManifest,
    build_directory_manifest,
)
from configuration_service.application.ports import (
    BackupPort,
    DocumentCodecPort,
    RepositoryPort,
)
from configuration_service.domain.models import (
    CommitResult,
    ConfigIdentity,
    PreparedDocument,
    PreparedVersion,
    RepositoryCapacityExceededError,
    RepositoryConflictError,
    RepositoryNotFoundError,
)


@dataclass(frozen=True, slots=True)
class RepositoryLimits:
    max_request_bytes: int
    max_bundle_bytes: int
    max_dependencies: int
    max_documents_per_publication: int
    candidate_ttl_seconds: int
    max_candidates: int
    max_backup_jobs: int
    page_size: int = 100

    def __post_init__(self) -> None:
        if (
            min(
                self.max_request_bytes,
                self.max_bundle_bytes,
                self.max_dependencies,
                self.max_documents_per_publication,
                self.candidate_ttl_seconds,
                self.max_candidates,
                self.max_backup_jobs,
                self.page_size,
            )
            < 1
        ):
            raise ValueError("repository limits must all be positive")


@dataclass(frozen=True, slots=True)
class DocumentInput:
    identity: ConfigIdentity
    file_name: str
    yaml_bytes: bytes
    relative_path: str = ""


@dataclass(frozen=True, slots=True)
class DocumentOutput:
    identity: ConfigIdentity
    version_id: str
    file_version: str
    schema_version: int
    revision_id: int
    schema_ref: str
    content_sha256: str
    yaml_sha256: str
    yaml_bytes: bytes
    file_name: str
    relative_path: str


class ConfigurationApplication:
    """Coordinates validated documents, immutable versions, and YAML reconstruction."""

    def __init__(
        self,
        repository: RepositoryPort,
        *,
        limits: RepositoryLimits,
        backups: BackupPort,
        own_service_id: str,
        codec: DocumentCodecPort,
    ) -> None:
        self.repository = repository
        self.codec = codec
        self.limits = limits
        self.backups = backups
        self.own_service_id = own_service_id

    def register_schema(
        self,
        *,
        config_type: str,
        document_schema_version: int,
        fields: dict[str, Any],
        header: dict[str, Any],
    ) -> str:
        """Install a trusted, software-delivered schema before admitting documents."""
        digest = self.codec.semantic_hash(
            {"config_type": config_type, "version": document_schema_version, "fields": fields}
        )
        schema_ref = f"{config_type}:{document_schema_version}:{digest[:16]}"
        self.repository.register_schema(
            schema_ref=schema_ref,
            config_type=config_type,
            document_schema_version=document_schema_version,
            schema_definition=fields,
            header_definition=header,
        )
        return schema_ref

    def _schema_for(self, config_type: str, schema_version: int) -> dict[str, Any]:
        return self.repository.get_schema_by_type(config_type, schema_version)

    @staticmethod
    def _validate_schema(document: dict[str, Any], schema: dict[str, Any]) -> None:
        """Enforce registered nested field rules without implicit type coercion."""

        def validate(value: Any, rule: dict[str, Any], path: str) -> None:
            kinds = rule.get("type")
            if kinds is not None:
                choices = kinds if isinstance(kinds, list) else [kinds]
                known = {
                    "string": lambda item: type(item) is str,
                    "integer": lambda item: type(item) is int,
                    "boolean": lambda item: type(item) is bool,
                    "number": lambda item: type(item) in (int, float),
                    "array": lambda item: isinstance(item, list),
                    "object": lambda item: isinstance(item, dict),
                    "null": lambda item: item is None,
                }
                if not all(kind in known for kind in choices):
                    raise ValueError(f"unsupported schema type at {path}")
                if not any(known[kind](value) for kind in choices):
                    raise ValueError(f"invalid field type: {path}")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"invalid enum value: {path}")
            if isinstance(value, list):
                items = rule.get("items")
                if not isinstance(items, dict):
                    raise ValueError(f"array schema requires items at {path}")
                for index, item in enumerate(value):
                    validate(item, items, f"{path}[{index}]")
                return
            if not isinstance(value, dict):
                return
            properties = rule.get("properties")
            if properties is None:
                return
            if not isinstance(properties, dict):
                raise ValueError(f"invalid nested schema at {path}")
            required = rule.get("required", [])
            if not isinstance(required, list):
                raise ValueError(f"invalid required fields at {path}")
            for key in required:
                if key not in value:
                    raise ValueError(f"missing required field: {path}.{key}")
            if rule.get("additionalProperties", False) is False:
                unknown = set(value) - set(properties)
                if unknown:
                    raise ValueError(f"unknown configuration fields at {path}: {sorted(unknown)}")
            for key, item in value.items():
                child = properties.get(key)
                if child is None:
                    continue
                if not isinstance(child, dict):
                    raise ValueError(f"invalid schema entry at {path}.{key}")
                validate(item, child, f"{path}.{key}")

        validate(document, schema, "document")

    def _prepare(self, item: DocumentInput) -> PreparedDocument:
        if not item.file_name.endswith(".yaml") or "/" in item.file_name:
            raise ValueError("configuration filename must be a local YAML filename")
        raw = self.codec.parse_yaml(item.yaml_bytes, max_bytes=self.limits.max_request_bytes)
        if item.identity.config_type == "service" and (
            item.identity.config_id == self.own_service_id
            or raw.get("service_id") == self.own_service_id
        ):
            raise ValueError("Configuration Service bootstrap must remain local YAML")
        registered = self._schema_for(item.identity.config_type, int(raw["schema_version"]))
        self._validate_schema(raw, json.loads(registered["schema_json"]))
        if item.identity.config_type == "units":
            from core.application.errors import ConfigError
            from core.configuration.units import parse_unit_catalog

            try:
                parse_unit_catalog({"quantities": raw["quantities"], "units": raw["units"]})
            except (ConfigError, KeyError) as exc:
                raise ValueError(f"invalid unit catalog: {exc}") from exc
        return self.codec.prepare_document(
            item.identity,
            item.yaml_bytes,
            schema_ref=str(registered["schema_ref"]),
            schema_version=int(registered["document_schema_version"]),
            max_bytes=self.limits.max_request_bytes,
        )

    def prepare_directory(
        self, documents: tuple[DirectoryFile, ...]
    ) -> tuple[DirectoryManifest, tuple[PreparedDocument, ...]]:
        """Validate a complete directory without admitting a partial publication."""
        manifest = build_directory_manifest(
            documents,
            max_documents=self.limits.max_documents_per_publication,
            max_bytes=self.limits.max_request_bytes,
        )
        prepared: list[PreparedDocument] = []
        seen: set[tuple[ConfigIdentity, str]] = set()
        for item in manifest.files:
            raw = self.codec.parse_yaml(
                item.yaml_bytes, max_bytes=self.limits.max_request_bytes
            )
            location = resolve_publication_identity(item.relative_path, raw)
            identity = ConfigIdentity(location.config_type, location.config_id)
            key = (identity, location.file_version)
            if key in seen:
                raise ValueError("duplicate configuration document identity and version")
            seen.add(key)
            prepared.append(
                self._prepare(
                    DocumentInput(
                        identity=identity,
                        file_name=item.relative_path.rsplit("/", 1)[-1],
                        yaml_bytes=item.yaml_bytes,
                        relative_path=item.relative_path,
                    )
                )
            )
        return manifest, tuple(prepared)

    def create_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        expected_revision: int,
        documents: tuple[DocumentInput, ...],
        validate_only: bool,
    ) -> tuple[str, tuple[PreparedDocument, ...]]:
        """Stage exactly one bounded, complete root snapshot, never a YAML patch."""
        if not documents:
            raise ValueError("full-directory publication cannot be empty")
        paths = tuple(
            DirectoryFile(item.relative_path, item.yaml_bytes) for item in documents
        )
        manifest, prepared = self.prepare_directory(paths)
        submitted = {item.relative_path: item for item in documents}
        for file, document in zip(manifest.files, prepared, strict=True):
            item = submitted[file.relative_path]
            if item.identity != document.identity:
                raise ValueError("caller ConfigId does not match the source YAML path")
            if item.file_name != file.relative_path.rsplit("/", 1)[-1]:
                raise ValueError("candidate filename must match its relative_path")
        self._compile_versions(prepared, expected_revision)
        if validate_only:
            return "", prepared
        candidate_id = self.repository.create_candidate(
            actor_id=actor_id,
            request_id=request_id,
            expected_revision=expected_revision,
            documents=prepared,
            manifest=manifest,
            ttl_seconds=self.limits.candidate_ttl_seconds,
            max_candidates=self.limits.max_candidates,
        )
        return candidate_id, prepared

    def publish_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        expected_revision: int,
        candidate_id: str,
    ) -> CommitResult:
        staged = self.repository.get_candidate(candidate_id, actor_id)
        documents: list[PreparedDocument] = []
        for item in staged["documents"]:
            payload = json.loads(str(item["canonical_payload_json"]))
            identity = ConfigIdentity(str(item["config_type"]), str(item["config_id"]))
            documents.append(
                PreparedDocument(
                    identity=identity,
                    schema_ref=str(item["schema_ref"]),
                    file_version=str(item["file_version"]),
                    content_sha256=str(item["content_sha256"]),
                    canonical_payload=payload,
                )
            )
        return self.repository.publish_candidate(
            actor_id=actor_id,
            request_id=request_id,
            candidate_id=candidate_id,
            expected_revision=expected_revision,
            versions=self._compile_versions(documents, expected_revision),
        )

    def _compile_versions(
        self, documents: Sequence[PreparedDocument], expected_revision: int
    ) -> tuple[PreparedVersion, ...]:
        """Validate and bind exact typed references before persisting a candidate."""
        # Resolve all explicit version references before the publication transaction.
        # Existing target versions are immutable; the repository rechecks FK and
        # deprecation status atomically at commit.
        versions: list[PreparedVersion] = []
        new_ids: dict[tuple[str, str, str], str] = {}
        for doc in documents:
            key = (doc.identity.config_type, doc.identity.config_id, doc.file_version)
            if key in new_ids:
                raise ValueError("duplicate configuration identity/file_version")
            try:
                new_ids[key] = self.repository.resolve_version(doc.identity, doc.file_version)
            except RepositoryNotFoundError:
                new_ids[key] = uuid.uuid4().hex
        for document in documents:
            references: list[tuple[str, str]] = []
            sources = document.canonical_payload.get("config_sources", {})
            if isinstance(sources, list):
                for item in sources:
                    if not isinstance(item, dict):
                        raise ValueError("config_sources items must be mappings")
                    identity = ConfigIdentity(str(item["config_type"]), str(item["config_id"]))
                    file_version = str(item["file_version"])
                    target_id = new_ids.get(
                        (identity.config_type, identity.config_id, file_version)
                    ) or self.repository.resolve_version(identity, file_version)
                    references.append((target_id, "config_sources"))
            elif isinstance(sources, dict):
                references.extend(
                    self._resolve_legacy_sources(sources, documents, new_ids)
                )
            else:
                raise ValueError("config_sources must be a mapping or list")
            # Stable published interpretation of every PointTable is pinned
            # to exactly the standard catalog files in this complete directory.
            if document.identity.config_type in {"business_points", "point_table"}:
                required = (
                    ("units",)
                    if document.identity.config_type == "business_points"
                    else ("units", "business_points")
                )
                for config_type in required:
                    standards = [
                        item for item in documents
                        if item.identity.config_type == config_type
                        and item.identity.config_id == "standard"
                    ]
                    if not standards:
                        raise ValueError(f"missing standard {config_type} document")
                    declared = document.canonical_payload.get("config_sources", [])
                    pinned = [
                        source for source in declared
                        if isinstance(source, dict)
                        and source.get("config_type") == config_type
                        and source.get("config_id") == "standard"
                    ] if isinstance(declared, list) else []
                    if len(pinned) > 1:
                        raise ValueError(f"duplicate {config_type} standard binding")
                    if pinned:
                        standard_version = str(pinned[0].get("file_version", ""))
                        targets = [
                            item for item in standards
                            if item.file_version == standard_version
                        ]
                    else:
                        targets = standards
                    if len(targets) != 1:
                        raise ValueError(
                            f"ambiguous {config_type} catalog version; "
                            "pin file_version in config_sources"
                        )
                    target = targets[0]
                    references.append(
                        (
                            new_ids[(config_type, "standard", target.file_version)],
                            f"{document.identity.config_type}.standard",
                        )
                    )
            if document.identity.config_type in {"point_table", "device_model"}:
                reference_fields = (("parent", "point_table"),)
                # DeviceModel also pins its driver protocol and optional Task.
                if document.identity.config_type == "device_model":
                    reference_fields = (
                        ("point_table", "point_table"),
                        ("protocol", "protocol"),
                        ("task", "task"),
                    )
                for field_name, config_type in reference_fields:
                    ref = document.canonical_payload.get(field_name)
                    if ref is None:
                        continue
                    if not isinstance(ref, dict):
                        raise ValueError(f"invalid {field_name} template reference")
                    identity = ConfigIdentity(config_type, str(ref.get("id", "")))
                    file_version = str(ref.get("version", ""))
                    if not file_version:
                        raise ValueError(f"missing {field_name} template version")
                    key = (identity.config_type, identity.config_id, file_version)
                    target_id = new_ids.get(key)
                    if target_id is None:
                        target_id = self.repository.resolve_version(identity, file_version)
                    references.append((target_id, f"template.{field_name}"))
            device_bindings: dict[str, tuple[str, str, str, str]] = {}
            # Device instances reference exact model and point-table templates.
            # Resolve only identities present in the staged bundle or repository.
            if document.identity.config_type == "device":
                model_payloads = {
                    (doc.identity.config_id, doc.file_version): doc.canonical_payload
                    for doc in documents
                    if doc.identity.config_type == "device_model"
                }
                for device_id, raw_device in document.canonical_payload.get("devices", {}).items():
                    if not isinstance(raw_device, dict):
                        raise ValueError("device entries must be mappings")
                    model = raw_device.get("model")
                    if not isinstance(model, dict):
                        raise ValueError("device model reference must be a mapping")
                    model_id = str(model.get("id", ""))
                    model_version = str(model.get("version", ""))
                    if not model_id or not model_version:
                        raise ValueError("device model identity and version are required")
                    key = ("device_model", model_id, model_version)
                    model_vid = new_ids.get(key)
                    if model_vid is None:
                        try:
                            model_vid = self.repository.resolve_version(
                                ConfigIdentity("device_model", model_id), model_version
                            )
                        except RepositoryNotFoundError as exc:
                            raise ValueError("device model reference is absent") from exc
                    references.append((model_vid, "device.model"))
                    model_data = model_payloads.get((model_id, model_version))
                    if model_data is None:
                        stored = self.repository.get_projection_rows(model_vid, "config_entries")
                        roots = [
                            row
                            for row in stored
                            if row["object_type"] == "document" and row["object_id"] == "root"
                        ]
                        if len(roots) != 1:
                            raise RepositoryConflictError("model version has no canonical record")
                        model_data = json.loads(roots[0]["properties_json"])
                    table_ref = model_data.get("point_table", {})
                    if not isinstance(table_ref, dict):
                        raise ValueError("model point_table reference must be a mapping")
                    table_id = table_ref.get("id")
                    table_file_version = table_ref.get("version")
                    if not isinstance(table_id, str) or not isinstance(table_file_version, str):
                        raise ValueError("model point_table must contain id and version")
                    table_vid = new_ids.get(("point_table", table_id, table_file_version))
                    if table_vid is None:
                        try:
                            table_vid = self.repository.resolve_version(
                                ConfigIdentity("point_table", table_id), table_file_version
                            )
                        except RepositoryNotFoundError as exc:
                            raise ValueError("device PointTable reference is absent") from exc
                    references.append((table_vid, "device.point_table"))
                    device_bindings[str(device_id)] = (
                        model_vid,
                        model_id,
                        table_vid,
                        f"point_table_{table_id}_v{table_file_version}",
                    )
            table_device_type: str | None = None
            if document.identity.config_type == "point_table":
                possible: set[str] = set()
                for model_document in documents:
                    if model_document.identity.config_type != "device_model":
                        continue
                    model_data = model_document.canonical_payload
                    table = model_data.get("point_table")
                    if (
                        isinstance(table, dict)
                        and table.get("id") == document.identity.config_id
                        and table.get("version") == document.file_version
                    ):
                        device_type = model_data.get(
                            "device_type_id", model_data.get("device_type")
                        )
                        if isinstance(device_type, str) and device_type:
                            possible.add(device_type)
                if len(possible) > 1:
                    raise RepositoryConflictError("point table maps to conflicting device types")
                if possible:
                    table_device_type = next(iter(possible))
            version_types = {
                version_id: config_type
                for (config_type, _config_id, _file_version), version_id in new_ids.items()
            }
            dependency_versions: dict[str, str] = {}
            ambiguous_types: set[str] = set()
            for target_version_id, _relation in references:
                config_type = version_types.get(target_version_id)
                if config_type is None:
                    identity = self.repository.get_version_identity(target_version_id)
                    config_type = identity.config_type
                previous = dependency_versions.get(config_type)
                if previous is not None and previous != target_version_id:
                    ambiguous_types.add(config_type)
                dependency_versions[config_type] = target_version_id
            for config_type in ambiguous_types:
                del dependency_versions[config_type]
            point_table_parent: tuple[str, str] | None = None
            if document.identity.config_type == "point_table":
                raw_parent = document.canonical_payload.get("parent")
                if raw_parent is not None:
                    if not isinstance(raw_parent, dict):
                        raise ValueError("PointTable parent must be a versioned identity")
                    parent_id = str(raw_parent.get("id", ""))
                    parent_version = str(raw_parent.get("version", ""))
                    parent_vid = new_ids.get(("point_table", parent_id, parent_version))
                    if parent_vid is None:
                        parent_vid = self.repository.resolve_version(
                            ConfigIdentity("point_table", parent_id), parent_version
                        )
                    point_table_parent = (
                        parent_vid, f"point_table_{parent_id}_v{parent_version}"
                    )
            projections = self.codec.create_projections(
                document,
                dependency_versions=dependency_versions,
                device_bindings=device_bindings,
                table_device_type=table_device_type,
                point_table_parent=point_table_parent,
            )
            for projection in projections:
                columns = projection.columns
                if projection.table == "business_points":
                    references.append(
                        (str(columns["standard_unit_version_id"]), "business.unit")
                    )
                elif projection.table == "point_tables":
                    parent = columns.get("parent_version_id")
                    if parent is not None:
                        references.append((str(parent), "point_table.parent"))
                elif projection.table == "devices":
                    references.append((str(columns["model_version_id"]), "device.model"))
                    references.append(
                        (str(columns["point_table_version_id"]), "device.point_table")
                    )
                elif projection.table == "point_mappings":
                    references.append(
                        (str(columns["business_config_version_id"]), "point.business")
                    )
                    references.append((str(columns["source_unit_version_id"]), "point.unit"))
            versions.append(
                PreparedVersion(
                    document=document,
                    version_id=new_ids[
                        (
                            document.identity.config_type,
                            document.identity.config_id,
                            document.file_version,
                        )
                    ],
                    projections=projections,
                    references=tuple(sorted(set(references))),
                )
            )
        return tuple(versions)

    def _resolve_legacy_sources(
        self,
        sources: dict[str, Any],
        documents: Sequence[PreparedDocument],
        new_ids: dict[tuple[str, str, str], str],
    ) -> list[tuple[str, str]]:
        """Resolve path-based loader sources to pinned versions at publication time."""
        from pathlib import PurePosixPath

        # A complete replacement never silently resolves to an omitted older file.
        selected: dict[ConfigIdentity, PreparedDocument] = {}
        for doc in documents:
            older = selected.get(doc.identity)
            if older is None or tuple(int(v) for v in doc.file_version.split(".")) > tuple(
                int(v) for v in older.file_version.split(".")
            ):
                selected[doc.identity] = doc
        staged = {
            identity: new_ids[
                (identity.config_type, identity.config_id, doc.file_version)
            ]
            for identity, doc in selected.items()
        }
        resolved: list[tuple[str, str]] = []
        instances = sources.get("instances", {})
        if not isinstance(instances, dict):
            raise ValueError("config_sources.instances must be a mapping")
        name_types = {
            "site": "site",
            "devices": "device",
            "sinks": "sink",
            "deliveries": "delivery",
            "tasks": "task",
        }
        for name, raw_path in instances.items():
            if name not in name_types or not isinstance(raw_path, str):
                raise ValueError("unsupported config_sources instance reference")
            # The key describes the role; simulation may select devices_sim.
            # Only published instance names are accepted, never arbitrary paths.
            allowed_names = {"devices": {"devices", "devices_sim"}}.get(
                name, {name}
            )
            path = PurePosixPath(raw_path)
            if (
                path.parent.as_posix() != "configs/instances"
                or path.suffix != ".yaml"
                or path.stem not in allowed_names
                or path.as_posix() != raw_path
            ):
                raise ValueError(
                    f"invalid config_sources.instances.{name} path: {raw_path}"
                )
            identity = ConfigIdentity(name_types[name], path.stem)
            version_id = staged.get(identity)
            if version_id is None:
                raise RepositoryConflictError(f"missing pinned configuration reference: {raw_path}")
            resolved.append((version_id, "config_sources.instances"))
        templates = sources.get("templates", {})
        if not isinstance(templates, dict):
            raise ValueError("config_sources.templates must be a mapping")
        if templates:
            if set(templates) != {"root"} or not isinstance(templates["root"], str):
                raise ValueError("unsupported config_sources.templates mapping")
            root = PurePosixPath(templates["root"])
            if root.is_absolute() or ".." in root.parts:
                raise ValueError("config_sources.templates.root must be a safe relative path")
            template_roots = {
                "configs/templates": frozenset(
                    {
                        "units",
                        "business_points",
                        "point_table",
                        "device_model",
                        "protocol",
                        "task",
                        "delivery",
                        "sink",
                        "observability",
                    }
                )
            }
            template_types = template_roots.get(root.as_posix())
            if template_types is None:
                raise RepositoryConflictError(
                    "unrecognized templates.root; explicit pinned references required"
                )
            template_versions = [
                version_id
                for identity, version_id in staged.items()
                if identity.config_type in template_types
            ]
            if not template_versions:
                raise RepositoryConflictError("template root has no registered pinned templates")
            resolved.extend(
                (version_id, "config_sources.templates") for version_id in sorted(template_versions)
            )
        if set(sources) - {"instances", "templates"}:
            raise ValueError("unknown config_sources groups")
        return sorted(set(resolved))

    def _restore(self, row: dict[str, Any], *, revision_id: int) -> DocumentOutput:
        """Return the exact uploaded YAML with verified semantic content and path."""
        version_id = str(row["version_id"])
        schema = self.repository.get_document_schema(str(row["schema_ref"]))
        expected = self.codec.reconstruct_document(
            self.repository.get_projection_rows(version_id, "config_entries"),
            expected_sha256=str(row["content_sha256"]),
        )
        self._validate_schema(expected, json.loads(str(schema["schema_json"])))
        source = self.repository.get_version_source(
            version_id, revision_id=revision_id
        )
        if source is None:
            raise RepositoryConflictError(
                "published configuration version has no source YAML file"
            )
        relative_path, data, yaml_sha256 = source
        if len(data) > self.limits.max_bundle_bytes:
            raise RepositoryCapacityExceededError("configuration YAML exceeds response limit")
        restored = self.codec.parse_yaml(data, max_bytes=self.limits.max_bundle_bytes)
        if self.codec.canonical_json(expected) != self.codec.canonical_json(restored):
            raise RepositoryConflictError(
                "immutable source YAML and stored canonical projection differ"
            )
        identity = ConfigIdentity(str(row["config_type"]), str(row["config_id"]))
        return DocumentOutput(
            identity=identity,
            version_id=version_id,
            file_version=str(row["file_version"]),
            schema_version=int(schema["document_schema_version"]),
            revision_id=revision_id,
            schema_ref=str(row["schema_ref"]),
            content_sha256=str(row["content_sha256"]),
            yaml_sha256=yaml_sha256,
            yaml_bytes=data,
            file_name=relative_path.rsplit("/", 1)[-1],
            relative_path=relative_path,
        )

    @staticmethod
    def _unique_export_paths(
        documents: tuple[DocumentOutput, ...],
    ) -> tuple[DocumentOutput, ...]:
        """Assign collision-free paths to multiple immutable versions in one bundle.

        The original on-disk YAML identity and bytes remain untouched. Only
        bundle export names change when two pinned versions share a source path.
        """
        counts: dict[str, int] = {}
        for item in documents:
            counts[item.relative_path] = counts.get(item.relative_path, 0) + 1
        exported: list[DocumentOutput] = []
        used: set[str] = set()
        for item in documents:
            if counts[item.relative_path] > 1:
                path = f"_bundle_versions/{item.version_id}/{item.file_name}"
            else:
                path = item.relative_path
            if path in used or len(path.encode("utf-8")) > 512:
                raise RepositoryConflictError("ambiguous historical bundle path")
            used.add(path)
            exported.append(replace(item, relative_path=path))
        return tuple(exported)

    def get_config(
        self,
        identity: ConfigIdentity,
        *,
        version_id: str | None = None,
        revision_id: int | None = None,
    ) -> DocumentOutput:
        record = self.repository.get_version(
            identity, version_id=version_id, revision_id=revision_id
        )
        result = self._restore(record, revision_id=revision_id or 0)
        if len(result.yaml_bytes) > self.limits.max_bundle_bytes:
            raise RepositoryCapacityExceededError("configuration YAML response exceeds limit")
        return result

    def get_bundle(
        self,
        roots: tuple[ConfigIdentity, ...],
        revision_id: int,
        *,
        include_dependencies: bool,
    ) -> tuple[DocumentOutput, ...]:
        if include_dependencies:
            records = self.repository.get_bundle_versions(
                roots, revision_id, max_versions=self.limits.max_dependencies
            )
        else:
            records = tuple(
                self.repository.get_version(identity, revision_id=revision_id) for identity in roots
            )
        output = self._unique_export_paths(
            tuple(self._restore(row, revision_id=revision_id) for row in records)
        )
        if sum(len(item.yaml_bytes) for item in output) > self.limits.max_bundle_bytes:
            raise RepositoryCapacityExceededError("configuration bundle exceeds response limit")
        return output

    def get_point_table_bundle(
        self,
        point_table_id: str,
        *,
        expected_manifest_sha256: str | None = None,
    ) -> tuple[tuple[DocumentOutput, ...], str]:
        """Retrieve historical immutable semantics, never the latest recommended mapping."""
        rows = self.repository.get_point_table_versions(
            point_table_id,
            max_versions=self.limits.max_dependencies,
        )
        if not rows:
            raise RepositoryNotFoundError("PointTable has no immutable closure")
        records = self._unique_export_paths(
            tuple(self._restore(row, revision_id=0) for row in rows)
        )
        kinds = {item.identity.config_type for item in records}
        if not {"point_table", "business_points", "units"}.issubset(kinds):
            raise RepositoryConflictError(
                "PointTable lacks a fixed BusinessPoint/Units interpretation closure"
            )
        if sum(len(item.yaml_bytes) for item in records) > self.limits.max_bundle_bytes:
            raise RepositoryCapacityExceededError("PointTable bundle exceeds response limit")
        entries = [
            {
                "config_type": item.identity.config_type,
                "config_id": item.identity.config_id,
                "version_id": item.version_id,
                "content_sha256": item.content_sha256,
                "yaml_sha256": item.yaml_sha256,
                "relative_path": item.relative_path,
            }
            for item in records
        ]
        entries.sort(
            key=lambda item: (item["config_type"], item["config_id"], item["version_id"])
        )
        manifest = hashlib.sha256(
            self.codec.canonical_json(entries).encode("utf-8")
        ).hexdigest()
        if expected_manifest_sha256 and expected_manifest_sha256 != manifest:
            raise RepositoryConflictError("PointTable immutable manifest hash mismatch")
        return records, manifest

    def create_backup(self, actor_id: str, request_id: str, label: str) -> str:
        return self.repository.create_backup_task(
            actor_id=actor_id,
            request_id=request_id,
            label=label,
            max_active_jobs=self.limits.max_backup_jobs,
        )

    def run_backup(self, task_id: str) -> None:
        self.backups.run(task_id)
