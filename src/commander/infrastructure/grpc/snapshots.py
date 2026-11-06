"""Candidate configuration snapshots with optimistic revision and safe file commits."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

import yaml

from commander.application.config_manager import _validate_unique_connections
from commander.application.service_lifecycle import CommanderService
from commander.infrastructure.config_commit import commit_config_files, recover_config_commit
from commander.infrastructure.service_settings import load_service_settings
from contracts import common_pb2
from core.application.errors import ConfigError
from configuration.shared_documents import SharedConfigPaths, SharedDocumentLoader
from configuration.shared_snapshot import build_shared_snapshot


@dataclass(frozen=True, slots=True)
class _Candidate:
    base_revision: str
    files: dict[str, bytes]
    revision: str


def _digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for key, content in sorted(files.items()):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


class ConfigSnapshots:
    """Disk/candidate config snapshots; never expose service secrets through Get."""

    def __init__(self, service: CommanderService, service_config: Path) -> None:
        self._service = service
        self._service_config = service_config.resolve(strict=True)
        if load_service_settings(self._service_config) != service.config_manager.active_settings:
            raise RuntimeError(
                "Commander service YAML changed during startup; restart before serving RPC"
            )
        self._startup_service_content = self._service_config.read_bytes()
        self._root = self._service_config.parent.parent
        recover_config_commit(self._root)
        self._lock = asyncio.Lock()
        self._candidate: dict[str, _Candidate] = {}

    def _relative(self, path: Path) -> str:
        target = path.resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"configuration path outside snapshot root: {path}")
        return target.relative_to(self._root).as_posix()

    def _target(self, name: str) -> Path:
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or not relative.parts
            or relative.as_posix() != name
            or any(part in (".", "..") for part in relative.parts)
            or relative.parts[0].startswith(".")
        ):
            raise ValueError(f"unsafe snapshot relative_path: {name!r}")
        target = self._root.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(self._root):
            raise ValueError(f"snapshot path escapes configuration root: {name}")
        return target

    def _allowed(self, name: str, devices: str, service_file: str, templates_root: Path) -> bool:
        if name in (devices, service_file):
            return True
        target = self._target(name)
        return target.suffix == ".yaml" and target.is_relative_to(templates_root)

    def _collect(self) -> dict[str, bytes]:
        """Read service and referenced files in one cooperative shared-lock scope."""
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return self._collect_locked()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _collect_locked(self) -> dict[str, bytes]:
        settings = load_service_settings(self._service_config)
        paths = (
            self._service_config,
            settings.devices_path,
            settings.templates_root / "units.yaml",
            settings.templates_root / "business_points.yaml",
        )
        files = {self._relative(path): path.read_bytes() for path in paths}
        documents = SharedDocumentLoader(
            SharedConfigPaths(
                instances=settings.devices_path.parent,
                templates=settings.templates_root,
                devices_file=settings.devices_path,
                load_site=False,
                coordination_root=self._root,
            )
        ).load()
        for definition in documents.devices.values():
            templates = definition.templates
            for item in (
                templates.model,
                templates.protocol,
                templates.point_table,
                *templates.inherited_tables,
            ):
                files[self._relative(item.path)] = item.path.read_bytes()
        refreshed = {name: self._target(name).read_bytes() for name in files}
        if files != refreshed:
            raise RuntimeError("configuration files changed while building disk snapshot")
        verify = SharedDocumentLoader(
            SharedConfigPaths(
                instances=settings.devices_path.parent,
                templates=settings.templates_root,
                devices_file=settings.devices_path,
                load_site=False,
                coordination_root=self._root,
            )
        ).load()
        if documents.revision != verify.revision:
            raise RuntimeError("configuration references changed during disk snapshot")
        return files

    def _runtime_files(self) -> dict[str, bytes]:
        """Serialize effective accepted configuration, including partial device reloads.

        This is a normalized runtime representation rather than original YAML
        formatting; it never substitutes current disk bytes for old bindings.
        """
        manager = self._service.config_manager
        documents = manager.documents
        settings = manager.active_settings
        devices: dict[str, dict[str, object]] = {}
        for device_id, definition in sorted(documents.devices.items()):
            model = definition.templates.model.reference
            item: dict[str, object] = {
                "model": {"id": model.template_id, "version": model.file_version},
                "endpoint": dict(definition.endpoint),
                "enabled": definition.enabled,
            }
            if definition.name is not None:
                item["name"] = definition.name
            if definition.protocol_options:
                item["overrides"] = {"protocol_options": dict(definition.protocol_options)}
            devices[device_id] = item
        files = {
            self._relative(self._service_config): self._startup_service_content,
            self._relative(settings.devices_path): yaml.safe_dump(
                {
                    "schema_version": 1,
                    "file_version": documents.devices_file_version,
                    "devices": devices,
                },
                sort_keys=False,
            ).encode("utf-8"),
            self._relative(settings.templates_root / "units.yaml"): yaml.safe_dump(
                dict(documents.units), sort_keys=False
            ).encode("utf-8"),
            self._relative(settings.templates_root / "business_points.yaml"): yaml.safe_dump(
                dict(documents.business_points), sort_keys=False
            ).encode("utf-8"),
        }
        for definition in documents.devices.values():
            templates = definition.templates
            for template in (
                templates.model,
                templates.protocol,
                templates.point_table,
                *templates.inherited_tables,
            ):
                files[self._relative(template.path)] = yaml.safe_dump(
                    dict(template.document), sort_keys=False
                ).encode("utf-8")
        return files

    @staticmethod
    def _file(name: str, content: bytes) -> common_pb2.SnapshotFile:
        return common_pb2.SnapshotFile(
            relative_path=name,
            content=content,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    async def get(
        self, request: common_pb2.GetConfigSnapshotRequest
    ) -> common_pb2.GetConfigSnapshotResponse:
        async with self._lock:
            if request.scope == common_pb2.SNAPSHOT_SCOPE_CANDIDATE:
                candidate = self._candidate.get(request.candidate_id)
                if candidate is None:
                    raise KeyError(request.candidate_id)
                return common_pb2.GetConfigSnapshotResponse(
                    snapshot_id=request.candidate_id,
                    revision=candidate.revision,
                    files=[
                        self._file(name, content)
                        for name, content in sorted(candidate.files.items())
                    ],
                )
            if request.scope not in (
                common_pb2.SNAPSHOT_SCOPE_DISK,
                common_pb2.SNAPSHOT_SCOPE_RUNTIME,
            ):
                raise ValueError("unsupported snapshot scope")
            if request.scope == common_pb2.SNAPSHOT_SCOPE_RUNTIME:
                files = self._runtime_files()
                revision = self._service.config_manager.revision
            else:
                files = self._collect()
                revision = _digest(files)
            response = common_pb2.GetConfigSnapshotResponse(
                snapshot_id=str(uuid4()),
                revision=revision,
                files=[self._file(name, content) for name, content in sorted(files.items())],
            )
            if request.scope == common_pb2.SNAPSHOT_SCOPE_RUNTIME:
                manager = self._service.config_manager
                blocked = manager.blocked_devices()
                response.apply_results.extend(
                    common_pb2.ApplicationResult(
                        object_id=str(device_id),
                        status="BLOCKED" if str(device_id) in blocked else "APPLIED",
                        message=manager.device_revision(str(device_id)) or "",
                    )
                    for device_id in sorted(manager.snapshot.site.devices)
                )
                if blocked:
                    response.validation.append(
                        common_pb2.ValidationIssue(
                            path=self._relative(manager.active_settings.devices_path),
                            code="MIXED_EFFECTIVE_REVISION",
                            message=(
                                "Runtime files are normalized from retained per-device "
                                "bindings; blocked devices have not applied disk changes"
                            ),
                        )
                    )
            return response

    def _validate_transport_files(self, document: dict[str, object]) -> None:
        """Check deploy-time certificate files without copying private keys to snapshots."""
        raw = document.get("security")
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ValueError("security must be a mapping")
        paths: dict[str, Path] = {}
        for key in ("ca_cert", "server_cert", "server_key"):
            pathname = raw.get(key)
            if not isinstance(pathname, str) or not pathname.strip():
                raise ValueError(f"security.{key} pathname is required")
            path = Path(pathname)
            if not path.is_absolute():
                path = self._service_config.parent / path
            paths[key] = path.resolve(strict=True)
            if not paths[key].is_file():
                raise ValueError(f"security.{key} is not a file")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_verify_locations(cafile=str(paths["ca_cert"]))
        context.load_cert_chain(
            certfile=str(paths["server_cert"]),
            keyfile=str(paths["server_key"]),
        )

    def _validate(self, files: dict[str, bytes]) -> None:
        # Validate the entire candidate against an isolated copy of shared
        # inputs, not against partly changed production files.
        with tempfile.TemporaryDirectory(prefix="commander-config-") as directory:
            root = Path(directory)
            service_relative = self._relative(self._service_config)
            for name, content in files.items():
                path = root.joinpath(*PurePosixPath(name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            service_path = root.joinpath(*PurePosixPath(service_relative).parts)
            document = yaml.safe_load(service_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("candidate Commander service document must be a mapping")
            self._validate_transport_files(document)
            sources = document.get("config_sources")
            if not isinstance(sources, dict):
                raise ValueError("candidate config_sources must be a mapping")
            # Absolute paths are valid in the deployed YAML. Resolve them against
            # the real service directory, then map to the isolated candidate tree.
            for section, key in (("instances", "devices"), ("templates", "root")):
                mapping = sources.get(section)
                if not isinstance(mapping, dict):
                    raise ValueError(f"candidate config_sources.{section} must be a mapping")
                pathname = mapping.get(key)
                if not isinstance(pathname, str) or not pathname.strip():
                    raise ValueError(f"candidate {section}.{key} pathname is required")
                original = Path(pathname)
                if not original.is_absolute():
                    original = self._service_config.parent / original
                relative = self._relative(original)
                staged = root.joinpath(*PurePosixPath(relative).parts)
                mapping[key] = str(staged)
            service_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            settings = load_service_settings(service_path)
            docs = SharedDocumentLoader(
                SharedConfigPaths(
                    instances=settings.devices_path.parent,
                    templates=settings.templates_root,
                    devices_file=settings.devices_path,
                    load_site=False,
                    coordination_root=root,
                )
            ).load()
            snapshot = build_shared_snapshot(docs)
            _validate_unique_connections(snapshot)

    async def push(
        self, request: common_pb2.PushConfigSnapshotRequest
    ) -> common_pb2.PushConfigSnapshotResponse:
        async with self._lock:
            current = self._collect()
            revision = _digest(current)
            if request.expected_revision != revision:
                raise RuntimeError("expected_revision does not match disk revision")
            # Verify integrity before inspecting any untrusted candidate YAML.
            seen_paths: set[str] = set()
            for item in request.files:
                name = item.relative_path
                self._target(name)
                if name in seen_paths:
                    raise ValueError("duplicate SnapshotFile relative_path")
                seen_paths.add(name)
                content = bytes(item.content)
                if item.size_bytes != len(content):
                    raise ValueError(f"SnapshotFile size_bytes mismatch: {name}")
                if item.sha256 != hashlib.sha256(content).hexdigest():
                    raise ValueError(f"SnapshotFile sha256 mismatch: {name}")
            service_name = self._relative(self._service_config)
            settings = load_service_settings(self._service_config)
            devices_name = self._relative(settings.devices_path)
            additional_devices: set[str] = set()
            additional_template_roots: list[Path] = []
            service_update = next(
                (item for item in request.files if item.relative_path == service_name),
                None,
            )
            if service_update is not None:
                try:
                    preview = yaml.safe_load(bytes(service_update.content))
                except yaml.YAMLError as exc:
                    raise ValueError("invalid candidate service YAML") from exc
                if not isinstance(preview, dict):
                    raise ValueError("invalid candidate service document")
                config_sources = preview.get("config_sources")
                if not isinstance(config_sources, dict):
                    raise ValueError("candidate config_sources is required")
                instances = config_sources.get("instances")
                templates = config_sources.get("templates")
                if not isinstance(instances, dict) or not isinstance(templates, dict):
                    raise ValueError("invalid candidate config source sections")
                device_pathname = instances.get("devices")
                templates_pathname = templates.get("root")
                if not isinstance(device_pathname, str) or not device_pathname.strip():
                    raise ValueError("candidate devices pathname is required")
                if not isinstance(templates_pathname, str) or not templates_pathname.strip():
                    raise ValueError("candidate templates pathname is required")
                next_devices = Path(device_pathname)
                next_templates = Path(templates_pathname)
                if not next_devices.is_absolute():
                    next_devices = self._service_config.parent / next_devices
                if not next_templates.is_absolute():
                    next_templates = self._service_config.parent / next_templates
                additional_devices.add(self._relative(next_devices))
                additional_template_roots.append(next_templates.resolve())
            candidate = dict(current)
            for item in request.files:
                name = item.relative_path
                target = self._target(name)
                candidate_source = name in additional_devices or any(
                    target.suffix == ".yaml" and target.is_relative_to(root)
                    for root in additional_template_roots
                )
                if (
                    not self._allowed(name, devices_name, service_name, settings.templates_root)
                    and not candidate_source
                ):
                    raise ValueError(f"file is not part of Commander config sources: {name}")
                content = bytes(item.content)
                is_versioned_template = (
                    target.is_relative_to(settings.templates_root)
                    or any(target.is_relative_to(root) for root in additional_template_roots)
                ) and target.name not in {"units.yaml", "business_points.yaml"}
                if is_versioned_template and target.is_file() and target.read_bytes() != content:
                    raise ValueError(f"versioned template is immutable: {name}")
                candidate[name] = content
            try:
                await asyncio.to_thread(self._validate, candidate)
            except (
                ConfigError,
                OSError,
                RuntimeError,
                ValueError,
                yaml.YAMLError,
                ssl.SSLError,
            ) as exc:
                return common_pb2.PushConfigSnapshotResponse(
                    valid=False,
                    base_revision=revision,
                    validation=[
                        common_pb2.ValidationIssue(
                            path="candidate", code="INVALID_CONFIG", message=str(exc)
                        )
                    ],
                )
            candidate_id = str(uuid4())
            self._candidate.clear()
            self._candidate[candidate_id] = _Candidate(revision, candidate, _digest(candidate))
            return common_pb2.PushConfigSnapshotResponse(
                candidate_id=candidate_id,
                valid=True,
                base_revision=revision,
            )

    async def apply(
        self, request: common_pb2.ApplyConfigSnapshotRequest
    ) -> common_pb2.ApplyConfigSnapshotResponse:
        async with self._lock:
            operation = asyncio.create_task(self._apply_locked(request))
            try:
                result = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Never release the snapshot lock during a file commit/reload.
                await operation
                raise
            except Exception:
                self._service.metrics.config_failed()
                raise
            if any(item.status in {"FAILED", "BLOCKED"} for item in result.apply_results):
                self._service.metrics.config_failed()
            return result

    async def _apply_locked(
        self, request: common_pb2.ApplyConfigSnapshotRequest
    ) -> common_pb2.ApplyConfigSnapshotResponse:
        candidate = self._candidate.get(request.candidate_id)
        if candidate is None:
            raise KeyError(request.candidate_id)
        current = self._collect()
        revision = _digest(current)
        if (
            request.expected_revision != candidate.base_revision
            or revision != candidate.base_revision
        ):
            raise RuntimeError("configuration revision conflict")
        await asyncio.to_thread(self._validate, candidate.files)
        changed = {
            name: content
            for name, content in candidate.files.items()
            if name not in current or current[name] != content
        }
        # Durable multi-file transaction; interrupted updates recover on startup.
        await asyncio.to_thread(commit_config_files, self._root, changed)
        self._candidate.pop(request.candidate_id, None)
        try:
            report = await self._service.reload()
        except Exception as exc:
            return common_pb2.ApplyConfigSnapshotResponse(
                file_commit=_digest(self._collect()),
                effective_revision=self._service.config_manager.revision,
                restart_required=True,
                application_id=str(uuid4()),
                apply_results=[
                    common_pb2.ApplicationResult(
                        object_id="commander",
                        status="FAILED",
                        error_code="RELOAD_FAILED",
                        message=str(exc),
                    )
                ],
            )
        return common_pb2.ApplyConfigSnapshotResponse(
            file_commit=_digest(self._collect()),
            effective_revision=report.revision,
            restart_required=report.restart_required,
            application_id=str(uuid4()),
            apply_results=[
                common_pb2.ApplicationResult(
                    object_id=device,
                    status="BLOCKED" if device in report.blocked else "APPLIED",
                )
                for device in (*report.changed, *report.blocked)
            ],
        )
