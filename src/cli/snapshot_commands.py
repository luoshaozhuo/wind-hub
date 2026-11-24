"""CommonService snapshot commands, separate from Repository publication."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import typer
from google.protobuf.json_format import MessageToDict

from contracts import common_pb2, common_pb2_grpc
from core.configuration.publication_identity import validate_relative_path

_MAX_FILES = 1000
_MAX_BYTES = 64 * 1024 * 1024


async def _invoke(target: str, method: str, request: Any) -> Any:
    from .main import runtime

    session = runtime()
    stub = common_pb2_grpc.CommonServiceStub(session.channel(target))
    return await getattr(stub, method)(
        request,
        timeout=session.target(target).timeout,
        metadata=session.get_rpc_metadata(target, method),
    )


def _verify_snapshot(files: Any) -> list[tuple[str, bytes]]:
    if len(files) > _MAX_FILES:
        raise ValueError("snapshot exceeds maximum file count")
    seen: set[str] = set()
    total = 0
    result: list[tuple[str, bytes]] = []
    for item in files:
        name = item.relative_path
        validate_relative_path(name)
        if name.casefold() in seen:
            raise ValueError("duplicate snapshot path")
        seen.add(name.casefold())
        content = item.content
        total += len(content)
        if total > _MAX_BYTES or item.size_bytes != len(content):
            raise ValueError("snapshot size mismatch or capacity exceeded")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError("snapshot SHA-256 mismatch")
        result.append((name, content))
    return result


def _write_snapshot(destination: Path, files: Any) -> None:
    verified = _verify_snapshot(files)
    if destination.exists() or destination.is_symlink():
        raise ValueError("snapshot destination must not exist")
    parent = destination.absolute().parent
    if not parent.is_dir():
        raise ValueError("snapshot destination parent does not exist")
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=parent) as temp:
        staged = Path(temp)
        for name, data in verified:
            output = staged / name
            output.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        os.rename(staged, destination)


def get_snapshot(target: str, scope: str, output: Path | None, candidate_id: str = "") -> None:
    """Read a service's disk/runtime/candidate snapshot through CommonService."""
    normalized = scope.lower()
    scopes = {
        "disk": common_pb2.SNAPSHOT_SCOPE_DISK,
        "runtime": common_pb2.SNAPSHOT_SCOPE_RUNTIME,
        "candidate": common_pb2.SNAPSHOT_SCOPE_CANDIDATE,
    }
    if normalized not in scopes:
        raise typer.BadParameter("scope must be disk, runtime or candidate")
    if normalized == "candidate" and not candidate_id:
        raise typer.BadParameter("candidate scope requires --candidate-id")
    if normalized != "candidate" and candidate_id:
        raise typer.BadParameter("--candidate-id requires candidate scope")

    async def invoke() -> dict[str, Any]:
        result = await _invoke(
            target,
            "GetConfigSnapshot",
            common_pb2.GetConfigSnapshotRequest(
                scope=scopes[normalized],
                candidate_id=candidate_id,
            ),
        )
        if output is not None:
            _write_snapshot(output, result.files)
        else:
            _verify_snapshot(result.files)
        data: dict[str, Any] = MessageToDict(result, preserving_proto_field_name=True)
        data["files"] = [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in result.files
        ]
        if output is not None:
            data["output"] = str(output)
        return data

    from .main import _run

    _run("config get", invoke())
