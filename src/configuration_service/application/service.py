"""Configuration repository use cases independent of protobuf and transport."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
    RepositoryVersionExistsError,
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

    def create_candidate(
        self,
        *,
        actor_id: str,
        request_id: str,
        expected_revision: int,
        documents: tuple[DocumentInput, ...],
        validate_only: bool,
    ) -> tuple[str, tuple[PreparedDocument, ...]]:
        if len(documents) > self.limits.max_documents_per_publication:
            raise ValueError("candidate exceeds document count limit")
        if sum(len(doc.yaml_bytes) for doc in documents) > self.limits.max_request_bytes:
            raise ValueError("candidate exceeds byte limit")
        prepared = tuple(self._prepare(item) for item in documents)
        self._compile_versions(prepared, expected_revision)
        if validate_only:
            return "", prepared
        candidate_id = self.repository.create_candidate(
            actor_id=actor_id,
            request_id=request_id,
            expected_revision=expected_revision,
            documents=prepared,
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
        # Full-root uploads repeat unchanged YAML on each publication. Reuse
        # immutable version IDs rather than re-inserting a published file_version.
        new_ids: dict[tuple[str, str, str], str] = {}
        for doc in documents:
            key = (doc.identity.config_type, doc.identity.config_id, doc.file_version)
            if key in new_ids:
                raise ValueError("duplicate immutable configuration version")
            try:
                prior_id = self.repository.resolve_version(doc.identity, doc.file_version)
            except RepositoryNotFoundError:
                prior_id = None
            if prior_id is not None:
                prior = self.repository.get_version(doc.identity, version_id=prior_id)
                if (
                    prior["content_sha256"] != doc.content_sha256
                    or prior["schema_ref"] != doc.schema_ref
                ):
                    raise RepositoryVersionExistsError(
                        "existing immutable file_version has different content or schema"
                    )
            new_ids[key] = prior_id or uuid.uuid4().hex
        for document in documents:
            references: list[tuple[str, str]] = []
            sources = document.canonical_payload.get("config_sources", {})
            if isinstance(sources, list):
                for item in sources:
                    if not isinstance(item, dict) or set(item) != {
                        "config_type",
                        "config_id",
                        "file_version",
                    }:
                        raise ValueError("config_sources requires type, id and file_version")
                    if any(not isinstance(item[key], str) or not item[key] for key in item):
                        raise ValueError("config_sources identity and version must be strings")
                    identity = ConfigIdentity(item["config_type"], item["config_id"])
                    file_version = item["file_version"]
                    target_id = new_ids.get(
                        (identity.config_type, identity.config_id, file_version)
                    ) or self.repository.resolve_version(identity, file_version)
                    references.append((target_id, "config_sources"))
            elif isinstance(sources, dict):
                references.extend(
                    self._resolve_legacy_sources(sources, expected_revision, documents, new_ids)
                )
            else:
                raise ValueError("config_sources must be a mapping or list")
            # A model's protocol, point table and optional task are pinned
            # dependencies, not implicit references to a template directory.
            if document.identity.config_type == "device_model":
                for field, target_type in (
                    ("protocol", "protocol"),
                    ("point_table", "point_table"),
                    ("task", "task"),
                ):
                    target = document.canonical_payload.get(field)
                    if target is None:
                        continue
                    if not isinstance(target, dict):
                        raise ValueError(f"device model {field} must be a mapping")
                    target_name = target.get("id")
                    target_version = target.get("version")
                    if (
                        not isinstance(target_name, str)
                        or not target_name
                        or (not isinstance(target_version, str) or not target_version)
                    ):
                        raise ValueError(f"device model {field} must pin id and version")
                    model_target_id = new_ids.get((target_type, target_name, target_version))
                    if model_target_id is None:
                        try:
                            model_target_id = self.repository.resolve_version(
                                ConfigIdentity(target_type, target_name), target_version
                            )
                        except RepositoryNotFoundError as exc:
                            raise RepositoryConflictError(
                                f"missing {target_type} {target_name}@{target_version}"
                            ) from exc
                    references.append((model_target_id, f"device_model.{field}"))
            if document.identity.config_type in {"sink", "delivery"}:
                config_type = document.identity.config_type
                entries_name = "deliveries" if config_type == "delivery" else "sinks"
                entries = document.canonical_payload.get(entries_name, {})
                if not isinstance(entries, dict):
                    raise ValueError(f"{config_type} entries must be a mapping")
                for entry in entries.values():
                    if not isinstance(entry, dict):
                        raise ValueError(f"{config_type} entries must be mappings")
                    template_name = entry.get("template_id")
                    template_version = entry.get("template_version")
                    if (
                        not isinstance(template_name, str)
                        or not template_name
                        or not isinstance(template_version, str)
                        or not template_version
                    ):
                        raise ValueError(
                            f"{config_type} entries must pin template_id and template_version"
                        )
                    template_target_id = new_ids.get((config_type, template_name, template_version))
                    if template_target_id is None:
                        try:
                            template_target_id = self.repository.resolve_version(
                                ConfigIdentity(config_type, template_name), template_version
                            )
                        except RepositoryNotFoundError as exc:
                            raise RepositoryConflictError(
                                f"missing {config_type} template "
                                f"{template_name}@{template_version}"
                            ) from exc
                    references.append((template_target_id, f"{config_type}.template"))
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
                            raise RepositoryConflictError(
                                f"missing device model {model_id}@{model_version}"
                            ) from exc
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
                            raise RepositoryConflictError(
                                f"missing point table {table_id}@{table_file_version}"
                            ) from exc
                    references.append((table_vid, "device.point_table"))
                    device_bindings[str(device_id)] = (model_vid, model_id, table_vid, table_id)
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
                        and isinstance(model_data.get("device_type_id"), str)
                    ):
                        possible.add(model_data["device_type_id"])
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
                dependency_type = version_types.get(target_version_id)
                if dependency_type is None:
                    identity = self.repository.get_version_identity(target_version_id)
                    dependency_type = identity.config_type
                previous = dependency_versions.get(dependency_type)
                if previous is not None and previous != target_version_id:
                    ambiguous_types.add(dependency_type)
                dependency_versions[dependency_type] = target_version_id
            for config_type in ambiguous_types:
                del dependency_versions[config_type]
            projections = self.codec.create_projections(
                document,
                dependency_versions=dependency_versions,
                device_bindings=device_bindings,
                table_device_type=table_device_type,
            )
            for projection in projections:
                columns = projection.columns
                if projection.table == "semantic_points":
                    references.append((str(columns["standard_unit_version_id"]), "semantic.unit"))
                elif projection.table == "business_points":
                    references.append(
                        (str(columns["semantic_config_version_id"]), "business.semantic")
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
            # The active directory revision must own the entire referenced
            # closure. Historical immutable versions remain queryable, but
            # cannot silently satisfy a missing current-directory file.
            staged_version_ids = set(new_ids.values())
            if any(target not in staged_version_ids for target, _ in references):
                raise RepositoryConflictError(
                    "full-directory publication omitted a required dependency"
                )
            canonical_refs = tuple(sorted(set(references)))
            # Reusing a version with a different dependency graph would
            # silently reinterpret historical PointTable semantics.
            try:
                published = self.repository.resolve_version(
                    document.identity, document.file_version
                )
            except RepositoryNotFoundError:
                published = None
            if published is not None and (
                self.repository.get_version_references(published) != canonical_refs
            ):
                raise RepositoryConflictError(
                    "immutable config dependency graph changed: bump file_version"
                )
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
                    references=canonical_refs,
                )
            )
        return tuple(versions)

    def _resolve_legacy_sources(
        self,
        sources: dict[str, Any],
        revision_id: int,
        documents: Sequence[PreparedDocument],
        new_ids: dict[tuple[str, str, str], str],
    ) -> list[tuple[str, str]]:
        """Resolve path-based loader sources to pinned versions at publication time."""
        from pathlib import PurePosixPath

        staged = {
            doc.identity: new_ids[
                (doc.identity.config_type, doc.identity.config_id, doc.file_version)
            ]
            for doc in documents
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
            file_stem = PurePosixPath(raw_path).stem
            # Historical filename version is not part of repository config_id.
            # The immutable file_version lives in config_versions instead.
            base = file_stem
            if "_v" in base:
                prefix, sep, suffix = base.rpartition("_v")
                if (
                    sep
                    and all(part.isdecimal() for part in suffix.split("."))
                    and (len(suffix.split(".")) == 3)
                ):
                    base = prefix
            kind = name_types[name]
            # Legacy filenames are complete identities (devices_sim -> devices_sim).
            # Only the canonical singular type prefix is stripped when present.
            if base.startswith(kind + "_"):
                base = base[len(kind) + 1 :]
            identity = ConfigIdentity(kind, base)
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
                        "semantic_points",
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
            if not any(identity.config_type in template_types for identity in staged):
                raise RepositoryConflictError("template root has no staged templates")
            # A directory root is a discovery location, not an immutable dependency
            # on every template it contains. Pinned dependencies are expressed by
            # explicit config_sources entries or concrete device/template bindings.
        if set(sources) - {"instances", "templates"}:
            raise ValueError("unknown config_sources groups")
        return sorted(set(resolved))

    def _restore(self, row: dict[str, Any], *, revision_id: int) -> DocumentOutput:
        version_id = str(row["version_id"])
        schema = self.repository.get_document_schema(str(row["schema_ref"]))
        rows = self.repository.get_projection_rows(version_id, "config_entries")
        raw = self.codec.reconstruct_document(rows, expected_sha256=str(row["content_sha256"]))
        self._validate_schema(raw, json.loads(str(schema["schema_json"])))
        header = json.loads(str(schema["header_definition_json"]))
        data = self.codec.emit_yaml(raw, header)
        if len(data) > self.limits.max_bundle_bytes:
            raise RepositoryCapacityExceededError("configuration YAML response exceeds limit")
        reconstructed = self.codec.parse_yaml(data, max_bytes=self.limits.max_bundle_bytes)
        if self.codec.canonical_json(raw) != self.codec.canonical_json(reconstructed):
            raise RuntimeError("YAML reconstruction changed a configuration field or type")
        identity = ConfigIdentity(str(row["config_type"]), str(row["config_id"]))
        return DocumentOutput(
            identity=identity,
            version_id=version_id,
            file_version=str(row["file_version"]),
            schema_version=int(schema["document_schema_version"]),
            revision_id=revision_id,
            schema_ref=str(schema["schema_ref"]),
            content_sha256=str(row["content_sha256"]),
            yaml_sha256=hashlib.sha256(data).hexdigest(),
            yaml_bytes=data,
            file_name=(
                f"{identity.config_type}_{identity.config_id}_" f"v{row['file_version']}.yaml"
            ),
        )

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
        output = tuple(self._restore(row, revision_id=revision_id) for row in records)
        if sum(len(item.yaml_bytes) for item in output) > self.limits.max_bundle_bytes:
            raise RepositoryCapacityExceededError("configuration bundle exceeds response limit")
        return output

    def create_backup(self, actor_id: str, request_id: str, label: str) -> str:
        return self.repository.create_backup_task(
            actor_id=actor_id,
            request_id=request_id,
            label=label,
            max_active_jobs=self.limits.max_backup_jobs,
        )

    def run_backup(self, task_id: str) -> None:
        self.backups.run(task_id)
