"""Configuration Repository CLI commands with complete directory publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import typer
from google.protobuf.json_format import MessageToDict

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from core.configuration.publication_identity import (
    resolve_publication_identity,
    validate_relative_path,
)
from core.infrastructure.config import ConfigOperationError, parse_yaml

configuration = typer.Typer(help="Manage versioned configuration repository.")

_LOCAL_STORAGE_BOOTSTRAP = re.compile(
    r"services/storage_bootstrap_v" r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.yaml"
)


def _is_local_bootstrap(relative_path: str) -> bool:
    """Exclude only known process bootstraps from the published YAML closure."""
    return relative_path == "services/cli.example.yaml" or bool(
        _LOCAL_STORAGE_BOOTSTRAP.fullmatch(relative_path)
    )


def _request(method: str, request: Any, service: str) -> Any:
    """Use the configured service identity and existing CLI gRPC channel."""

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        client = rpc.ConfigurationRepositoryServiceStub(runtime().channel(service))
        result = await getattr(client, method)(
            request,
            timeout=runtime().target(service).timeout,
            metadata=runtime().get_rpc_metadata(service, method),
        )
        return MessageToDict(result, preserving_proto_field_name=True)

    from cli.main import _run

    _run(f"configuration {method}", invoke())


def _identity(config_type: str, config_id: str) -> pb.ConfigId:
    return pb.ConfigId(config_type=config_type, config_id=config_id)


@configuration.command("publish")
def publish(
    root: Path = typer.Option(..., "--root", help="Complete configuration directory"),
    service: str = "configuration",
    request_id: str = "",
    validate_only: bool = False,
) -> None:
    """Publish every Repository YAML under --root, excluding fixed local bootstraps."""
    if root.is_symlink() or not root.is_dir():
        raise typer.BadParameter("configuration root must be a real directory")
    root = root.resolve(strict=True)
    # Reject hidden directory links as well as linked files: pathlib.rglob
    # does not traverse symlinked directories, which would silently omit data.
    entries = tuple(root.rglob("*"))
    for entry in entries:
        if entry.is_symlink():
            raise typer.BadParameter(f"configuration tree contains symlink: {entry}")
        if entry.is_file() and entry.suffix == ".yml":
            raise typer.BadParameter(f"use canonical .yaml suffix: {entry}")
    paths = sorted(
        entry
        for entry in entries
        if entry.is_file()
        and entry.suffix == ".yaml"
        and not _is_local_bootstrap(entry.relative_to(root).as_posix())
    )
    if not paths or len(paths) > 1000:
        raise typer.BadParameter("configuration root must contain 1..1000 YAML files")
    documents: list[pb.ConfigDocumentInput] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for path in paths:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise typer.BadParameter(f"unsafe YAML path: {path}")
        relative_path = path.relative_to(root).as_posix()
        if relative_path.casefold() in seen_paths:
            raise typer.BadParameter(f"duplicate YAML path: {relative_path}")
        seen_paths.add(relative_path.casefold())
        content = path.read_bytes()
        total_bytes += len(content)
        if total_bytes > 64 * 1024 * 1024:
            raise typer.BadParameter("publication exceeds 64 MiB")
        try:
            raw = parse_yaml(content.decode("utf-8"))
            identity = resolve_publication_identity(relative_path, raw)
        except (UnicodeError, ValueError, KeyError, ConfigOperationError) as exc:
            raise typer.BadParameter(f"invalid configuration {relative_path}: {exc}") from exc
        documents.append(
            pb.ConfigDocumentInput(
                config_id=_identity(identity.config_type, identity.config_id),
                file_name=path.name,
                yaml_bytes=content,
                relative_path=relative_path,
            )
        )

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        session = runtime()
        client = rpc.ConfigurationRepositoryServiceStub(session.channel(service))
        metadata = session.get_rpc_metadata(service, "GetCurrentRevision")
        timeout = session.target(service).timeout
        current = await client.GetCurrentRevision(
            pb.GetCurrentRevisionRequest(), timeout=timeout, metadata=metadata
        )
        operation_id = request_id or uuid.uuid4().hex
        staged = await client.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id=f"{operation_id}-stage",
                expected_revision_id=current.revision_id,
                documents=documents,
                validate_only=validate_only,
            ),
            timeout=timeout,
            metadata=metadata,
        )
        if not staged.valid:
            raise RuntimeError(
                "complete configuration directory rejected: "
                + str(MessageToDict(staged, preserving_proto_field_name=True))
            )
        if validate_only:
            return {
                "valid": True,
                "base_revision_id": current.revision_id,
                "manifest_sha256": staged.manifest_sha256,
                "file_count": len(documents),
                "published": False,
            }
        committed = await client.PublishCandidate(
            pb.PublishCandidateRequest(
                candidate_id=staged.candidate_id,
                expected_revision_id=current.revision_id,
                request_id=f"{operation_id}-commit",
            ),
            timeout=timeout,
            metadata=metadata,
        )
        return {
            "revision_id": committed.revision_id,
            "already_applied": committed.already_applied,
            "manifest_sha256": staged.manifest_sha256,
            "file_count": len(documents),
        }

    from cli.main import _run

    _run("config publish", invoke())


@configuration.command("list")
def list_configs(
    service: str = "configuration",
    config_type: str = "",
    prefix: str = "",
    module_type: str = "",
    page_size: int = 100,
    page_token: str = "",
) -> None:
    _request(
        "ListConfigs",
        pb.ListConfigsRequest(
            config_type=config_type,
            config_id_prefix=prefix,
            module_type=module_type,
            page_size=page_size,
            page_token=page_token,
        ),
        service,
    )


@configuration.command("versions")
def versions(
    config_type: str,
    config_id: str,
    service: str = "configuration",
    include_deprecated: bool = False,
    page_token: str = "",
) -> None:
    _request(
        "ListConfigVersions",
        pb.ListConfigVersionsRequest(
            config_id=_identity(config_type, config_id),
            include_deprecated=include_deprecated,
            page_token=page_token,
        ),
        service,
    )


@configuration.command("get")
def get(
    config_type: str,
    config_id: str = "",
    output: Path | None = None,
    version_id: str = "",
    revision_id: int = -1,
    service: str = "configuration",
    scope: str = "",
    candidate_id: str = "",
) -> None:
    """Export Repository YAML, or explicitly query a service snapshot with --scope."""
    if scope:
        if config_id or version_id or revision_id >= 0:
            raise typer.BadParameter("snapshot scope cannot use Repository selectors")
        from .snapshot_commands import get_snapshot

        get_snapshot(config_type, scope, output, candidate_id)
        return
    if candidate_id:
        raise typer.BadParameter("--candidate-id requires --scope candidate")
    if not config_id or output is None:
        raise typer.BadParameter("Repository get requires config_id and output")
    if (not version_id) == (revision_id < 0):
        raise typer.BadParameter("provide exactly one of version_id or revision_id")
    req = pb.GetConfigRequest(config_id=_identity(config_type, config_id))
    if version_id:
        req.version_id = version_id
    else:
        req.revision_id = revision_id

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        client = rpc.ConfigurationRepositoryServiceStub(runtime().channel(service))
        result = await client.GetConfig(
            req,
            timeout=runtime().target(service).timeout,
            metadata=runtime().get_rpc_metadata(service, "GetConfig"),
        )
        data = result.document.yaml_bytes
        if hashlib.sha256(data).hexdigest() != result.document.yaml_sha256:
            raise RuntimeError("downloaded YAML SHA-256 mismatch")
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        return {"output": str(output), "version_id": result.document.version_id}

    from cli.main import _run

    _run("configuration get", invoke())


@configuration.command("bundle")
def bundle(
    config_type: str,
    config_id: str,
    revision_id: int,
    output: Path,
    service: str = "configuration",
    dependencies: bool = True,
) -> None:
    """Export a verified YAML dependency bundle to a new private directory."""

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        client = rpc.ConfigurationRepositoryServiceStub(runtime().channel(service))
        response = await client.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[_identity(config_type, config_id)],
                revision_id=revision_id,
                include_dependencies=dependencies,
            ),
            timeout=runtime().target(service).timeout,
            metadata=runtime().get_rpc_metadata(service, "GetConfigBundle"),
        )
        manifest = hashlib.sha256()
        names: set[str] = set()
        files: list[tuple[str, bytes]] = []
        for item in response.files:
            name = item.relative_path
            validate_relative_path(name)
            if name in names:
                raise ValueError("duplicate configuration path")
            data = item.yaml_bytes
            if hashlib.sha256(data).hexdigest() != item.yaml_sha256:
                raise RuntimeError("bundle contains incorrect YAML checksum")
            for value in (item.relative_path, item.version_id, item.yaml_sha256):
                encoded = value.encode("utf-8")
                manifest.update(len(encoded).to_bytes(4, "big"))
                manifest.update(encoded)
            names.add(name)
            files.append((name, data))
        if (
            manifest.hexdigest() != response.manifest_sha256
            or sum(len(data) for _name, data in files) != response.total_bytes
        ):
            raise RuntimeError("bundle manifest hash or total size mismatch")
        if output.exists() or output.is_symlink():
            raise FileExistsError("bundle output directory must not already exist")
        parent = output.absolute().parent
        with tempfile.TemporaryDirectory(prefix=".configuration-bundle-", dir=parent) as directory:
            staging = Path(directory)
            for name, data in files:
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            os.rename(staging, output)
        return {
            "directory": str(output),
            "revision_id": response.revision_id,
            "files": len(files),
            "total_bytes": response.total_bytes,
            "manifest_sha256": response.manifest_sha256,
        }

    from cli.main import _run

    _run("configuration bundle", invoke())


@configuration.command("revision")
def current_revision(service: str = "configuration") -> None:
    """Read the authoritative current directory revision and manifest digest."""
    _request("GetCurrentRevision", pb.GetCurrentRevisionRequest(), service)


@configuration.command("point-table-bundle")
def point_table_bundle(
    point_table_id: str,
    service: str = "configuration",
    expected_manifest_sha256: str = "",
) -> None:
    """Verify one immutable historical PointTable interpretation closure."""

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        session = runtime()
        client = rpc.ConfigurationRepositoryServiceStub(session.channel(service))
        result = await client.GetPointTableBundle(
            pb.GetPointTableBundleRequest(
                point_table_id=point_table_id,
                expected_manifest_sha256=expected_manifest_sha256,
            ),
            timeout=session.target(service).timeout,
            metadata=session.get_rpc_metadata(service, "GetPointTableBundle"),
        )
        paths: set[str] = set()
        entries: list[dict[str, str]] = []
        for doc in result.documents:
            validate_relative_path(doc.relative_path)
            if doc.relative_path in paths:
                raise RuntimeError("duplicate historical PointTable bundle path")
            if hashlib.sha256(doc.yaml_bytes).hexdigest() != doc.yaml_sha256:
                raise RuntimeError("historical PointTable YAML checksum mismatch")
            paths.add(doc.relative_path)
            entries.append(
                {
                    "config_type": doc.config_id.config_type,
                    "config_id": doc.config_id.config_id,
                    "version_id": doc.version_id,
                    "content_sha256": doc.content_sha256,
                    "yaml_sha256": doc.yaml_sha256,
                    "relative_path": doc.relative_path,
                }
            )
        entries.sort(key=lambda item: (item["config_type"], item["config_id"], item["version_id"]))
        actual = hashlib.sha256(
            json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if not entries or actual != result.manifest_sha256:
            raise RuntimeError("historical PointTable manifest checksum mismatch")
        return {
            "point_table_id": result.point_table_id,
            "device_type": result.device_type,
            "manifest_sha256": result.manifest_sha256,
            "documents": len(entries),
            "total_bytes": sum(len(doc.yaml_bytes) for doc in result.documents),
        }

    from cli.main import _run

    _run("configuration point-table-bundle", invoke())


@configuration.command("references")
def references(
    version_id: str,
    service: str = "configuration",
    inbound: bool = False,
    page_token: str = "",
) -> None:
    _request(
        "ListReferences",
        pb.ListReferencesRequest(
            version_id=version_id,
            direction=(
                pb.REFERENCE_DIRECTION_INBOUND if inbound else pb.REFERENCE_DIRECTION_OUTBOUND
            ),
            page_token=page_token,
        ),
        service,
    )


@configuration.command("recommend")
def recommend(
    config_type: str,
    config_id: str,
    version_id: str,
    expected_revision: int,
    service: str = "configuration",
    request_id: str = "",
) -> None:
    _request(
        "SetRecommendedVersion",
        pb.SetRecommendedVersionRequest(
            config_id=_identity(config_type, config_id),
            version_id=version_id,
            expected_revision_id=expected_revision,
            request_id=request_id or uuid.uuid4().hex,
        ),
        service,
    )


@configuration.command("deprecate")
def deprecate(
    version_id: str,
    reason: str,
    expected_revision: int,
    service: str = "configuration",
    request_id: str = "",
) -> None:
    _request(
        "DeprecateConfigVersion",
        pb.DeprecateConfigVersionRequest(
            version_id=version_id,
            reason=reason,
            expected_revision_id=expected_revision,
            request_id=request_id or uuid.uuid4().hex,
        ),
        service,
    )


@configuration.command("stats")
def stats(
    service: str = "configuration", config_type: str = "", include_storage: bool = True
) -> None:
    _request(
        "GetRepositoryStats",
        pb.GetRepositoryStatsRequest(
            scope=(
                pb.REPOSITORY_STATS_SCOPE_CONFIG_TYPE
                if config_type
                else pb.REPOSITORY_STATS_SCOPE_ALL
            ),
            config_type=config_type,
            include_storage=include_storage,
        ),
        service,
    )


@configuration.command("backup")
def backup(service: str = "configuration", label: str = "", request_id: str = "") -> None:
    _request(
        "CreateBackup",
        pb.CreateBackupRequest(request_id=request_id or uuid.uuid4().hex, label=label),
        service,
    )


@configuration.command("backup-status")
def backup_status(task_id: str, service: str = "configuration") -> None:
    _request("GetBackupTask", pb.GetBackupTaskRequest(task_id=task_id), service)
