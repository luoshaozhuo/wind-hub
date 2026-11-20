"""Configuration Repository CLI commands with scoped bearer-token gRPC."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path, PurePosixPath
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


@configuration.command("sync")
def sync_directory(
    root: Path,
    service: str = "configuration",
    request_id: str = "",
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Atomically replace the entire Configuration revision with one configs/ root."""
    from cli.configuration_directory import read_complete_directory
    from cli.safety import confirm_mutation

    documents = read_complete_directory(root)
    confirm_mutation(
        f"Replace Configuration current directory with {len(documents)} objects?",
        yes,
        command="configuration sync",
    )

    async def invoke() -> dict[str, Any]:
        from cli.main import runtime

        configured = runtime()
        client = rpc.ConfigurationRepositoryServiceStub(configured.channel(service))
        metadata = configured.get_rpc_metadata(service, "CreateCandidate")
        timeout = configured.target(service).timeout
        listing = await client.ListConfigs(
            pb.ListConfigsRequest(page_size=1),
            timeout=timeout,
            metadata=metadata,
        )
        revision = listing.snapshot_revision_id
        operation_id = request_id or uuid.uuid4().hex
        candidate = await client.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id=operation_id + ":candidate",
                expected_revision_id=revision,
                documents=[
                    pb.ConfigDocumentInput(
                        config_id=_identity(item.config_type, item.config_id),
                        file_name=item.filename,
                        yaml_bytes=item.content,
                    )
                    for item in documents
                ],
            ),
            timeout=timeout,
            metadata=metadata,
        )
        if not candidate.valid or not candidate.candidate_id:
            raise ValueError(
                "Configuration rejected complete root: "
                + str([issue.message for issue in candidate.issues])
            )
        published = await client.PublishCandidate(
            pb.PublishCandidateRequest(
                candidate_id=candidate.candidate_id,
                expected_revision_id=candidate.base_revision_id,
                request_id=operation_id + ":publish",
            ),
            timeout=timeout,
            metadata=metadata,
        )
        if not published.revision_id:
            raise RuntimeError("Configuration publish has no committed revision")
        return {
            "revision_id": published.revision_id,
            "files": len(documents),
            "already_applied": published.already_applied,
            "replacement": "complete",
        }

    from cli.main import _run

    _run("configuration sync", invoke())


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
            name = item.file_name
            relative = PurePosixPath(name)
            if (
                not name.endswith(".yaml")
                or name in names
                or relative.is_absolute()
                or relative.as_posix() != name
                or "\\" in name
                or any(part in {".", ".."} for part in relative.parts)
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
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
