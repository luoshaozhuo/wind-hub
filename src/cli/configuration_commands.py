"""Configuration Repository CLI commands, using the shared mTLS runtime."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import typer
from google.protobuf.json_format import MessageToDict

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc

configuration = typer.Typer(help="Manage versioned configuration repository.")


def _request(method: str, request: Any, service: str) -> Any:
    """Use the configured service identity and existing CLI gRPC channel."""

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        client = rpc.ConfigurationRepositoryServiceStub(runtime().channel(service))
        result = await getattr(client, method)(request)
        return MessageToDict(result, preserving_proto_field_name=True)

    from cli.main import _run

    _run(f"configuration {method}", invoke())


def _identity(config_type: str, config_id: str) -> pb.ConfigId:
    return pb.ConfigId(config_type=config_type, config_id=config_id)


@configuration.command("candidate")
def candidate(
    config_type: str,
    config_id: str,
    file: Path,
    expected_revision: int,
    service: str = "configuration",
    request_id: str = "",
    validate_only: bool = False,
) -> None:
    """Validate or stage a YAML document at a fixed repository revision."""
    data = file.read_bytes()
    _request(
        "CreateCandidate",
        pb.CreateCandidateRequest(
            request_id=request_id or uuid.uuid4().hex,
            expected_revision_id=expected_revision,
            documents=[
                pb.ConfigDocumentInput(
                    config_id=_identity(config_type, config_id),
                    file_name=file.name,
                    yaml_bytes=data,
                )
            ],
            validate_only=validate_only,
        ),
        service,
    )


@configuration.command("publish")
def publish(
    candidate_id: str,
    expected_revision: int,
    service: str = "configuration",
    request_id: str = "",
) -> None:
    """Publish a frozen candidate atomically, without applying business modules."""
    _request(
        "PublishCandidate",
        pb.PublishCandidateRequest(
            candidate_id=candidate_id,
            expected_revision_id=expected_revision,
            request_id=request_id or uuid.uuid4().hex,
        ),
        service,
    )


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
    config_id: str,
    output: Path,
    version_id: str = "",
    revision_id: int = -1,
    service: str = "configuration",
) -> None:
    """Save verified full-Header YAML from an exact version or revision."""
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
        result = await client.GetConfig(req)
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
            )
        )
        manifest = hashlib.sha256()
        names: set[str] = set()
        files: list[tuple[str, bytes]] = []
        for item in response.files:
            name = item.file_name
            if (
                not name.endswith(".yaml")
                or name in names
                or name != Path(name).name
                or "\\" in name
                or name in (".", "..")
            ):
                raise ValueError("duplicate or unsafe configuration filename")
            data = item.yaml_bytes
            if hashlib.sha256(data).hexdigest() != item.yaml_sha256:
                raise RuntimeError("bundle contains incorrect YAML checksum")
            manifest.update(item.version_id.encode("utf-8"))
            manifest.update(item.yaml_sha256.encode("ascii"))
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


@configuration.command("apply")
def apply_revision(
    target: str,
    manifest: Path,
    revision_id: int,
    repository: str = "configuration",
) -> None:
    """Push explicitly mapped repository YAML into one service's CommonService."""
    from core.infrastructure.config import parse_yaml

    raw = parse_yaml(manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise typer.BadParameter("manifest schema_version must be 1")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise typer.BadParameter("manifest requires at least one config file")

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime
        from contracts import common_pb2 as common
        from contracts import common_pb2_grpc as common_rpc

        source = rpc.ConfigurationRepositoryServiceStub(runtime().channel(repository))
        destination = common_rpc.CommonServiceStub(runtime().channel(target))
        disk = await destination.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
            metadata=runtime().get_rpc_metadata(target, "GetConfigSnapshot"),
        )
        snapshots: dict[str, common.SnapshotFile] = {
            item.relative_path: common.SnapshotFile(
                relative_path=item.relative_path,
                content=item.content,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in disk.files
        }
        if len(snapshots) != len(disk.files):
            raise ValueError("current disk snapshot contains duplicate paths")
        paths: set[str] = set()
        for entry in files:
            if not isinstance(entry, dict):
                raise ValueError("manifest file entries must be objects")
            relative_path = str(entry["relative_path"])
            normalized = Path(relative_path)
            if normalized.is_absolute() or ".." in normalized.parts or relative_path in paths:
                raise ValueError("manifest paths must be distinct and relative")
            paths.add(relative_path)
            config_id = _identity(str(entry["config_type"]), str(entry["config_id"]))
            query = pb.GetConfigRequest(config_id=config_id)
            if "version_id" in entry:
                query.version_id = str(entry["version_id"])
            else:
                query.revision_id = revision_id
            result = await source.GetConfig(query)
            content = result.document.yaml_bytes
            if hashlib.sha256(content).hexdigest() != result.document.yaml_sha256:
                raise RuntimeError("downloaded YAML digest mismatch")
            if relative_path not in snapshots:
                raise ValueError("target path must already exist in the disk snapshot")
            snapshots[relative_path] = common.SnapshotFile(
                relative_path=relative_path,
                content=content,
                size_bytes=len(content),
                sha256=result.document.yaml_sha256,
            )
        staged = await destination.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(
                files=[snapshots[path] for path in sorted(snapshots)],
                expected_revision=disk.revision,
            ),
            metadata=runtime().get_rpc_metadata(target, "PushConfigSnapshot"),
        )
        if not staged.valid:
            raise RuntimeError(
                "target rejected configuration snapshot: "
                + str(MessageToDict(staged, preserving_proto_field_name=True))
            )
        applied = await destination.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id=staged.candidate_id,
                expected_revision=disk.revision,
            ),
            metadata=runtime().get_rpc_metadata(target, "ApplyConfigSnapshot"),
        )
        return {
            "applied": True,
            "target": target,
            "repository_revision": revision_id,
            "result": MessageToDict(applied, preserving_proto_field_name=True),
        }

    from cli.main import _run

    _run("configuration apply", invoke())
