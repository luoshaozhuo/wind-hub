"""Cross-service configuration snapshots, diagnostics, metrics and events."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import typer
from google.protobuf.json_format import MessageToDict, ParseDict

config = typer.Typer(help="Remote configuration snapshots and local diff.")


async def request_common(service: str, method: str, values: dict[str, Any]) -> dict[str, Any]:
    """Call the frozen common contract with validated message fields."""
    from google.protobuf import message_factory

    from contracts import common_pb2, common_pb2_grpc

    from .main import runtime

    descriptor = common_pb2.DESCRIPTOR.services_by_name["CommonService"]
    info = descriptor.methods_by_name[method]
    request = ParseDict(values, message_factory.GetMessageClass(info.input_type)())
    stub = common_pb2_grpc.CommonServiceStub(runtime().channel(service))
    response = await getattr(stub, method)(
        request,
        timeout=runtime().target(service).timeout,
    )
    return MessageToDict(
        response,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )


def _run_common(service: str, method: str, values: dict[str, Any]) -> None:
    from .main import _run

    _run(f"{service} {method}", request_common(service, method, values))


def _snapshot_files(directory: Path, max_bytes: int) -> list[dict[str, Any]]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("snapshot source must be a regular directory")
    items: list[dict[str, Any]] = []
    total = 0
    for path in sorted(directory.rglob("*")):
        if len(items) >= 10000:
            raise ValueError("snapshot has too many files")
        if path.is_symlink():
            raise ValueError(f"snapshot contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        size = path.stat().st_size
        if size > max_bytes - total:
            raise ValueError("snapshot exceeds configured size limit")
        content = path.read_bytes()
        total += len(content)
        if total > max_bytes:
            raise ValueError("snapshot exceeded limit during read")
        import base64

        items.append(
            {
                "relative_path": relative,
                "content": base64.b64encode(content).decode("ascii"),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return items


@config.command("get")
def get_snapshot(
    service: str,
    scope: str = "disk",
    candidate_id: str = "",
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    if scope not in {"disk", "runtime", "candidate"}:
        from .main import runtime

        runtime().error(
            f"{service} config get", ValueError("scope must be disk, runtime or candidate")
        )
        raise typer.Exit(code=2)
    if scope == "candidate" and not candidate_id:
        from .main import runtime

        runtime().error(f"{service} config get", ValueError("candidate_id is required"))
        raise typer.Exit(code=2)
    request = {"scope": "SNAPSHOT_SCOPE_" + scope.upper(), "candidate_id": candidate_id}
    if output is None:
        from .main import _run

        async def metadata() -> dict[str, Any]:
            response = await request_common(service, "GetConfigSnapshot", request)
            files = response.pop("files", [])
            response["files"] = [
                {
                    "relative_path": item.get("relative_path"),
                    "size_bytes": item.get("size_bytes"),
                    "sha256": item.get("sha256"),
                }
                for item in files
                if isinstance(item, dict)
            ]
            return response

        _run(f"{service} config get", metadata())
        return

    from .main import _run, runtime
    from .snapshot_io import export_snapshot

    async def fetch() -> dict[str, Any]:
        response = await request_common(service, "GetConfigSnapshot", request)
        return export_snapshot(response, output, runtime().config().snapshot_max_bytes)

    _run(f"{service} config get", fetch())


async def _disk_revision(service: str) -> str:
    result = await request_common(
        service,
        "GetConfigSnapshot",
        {"scope": "SNAPSHOT_SCOPE_DISK"},
    )
    revision = result.get("revision")
    if not isinstance(revision, str) or not revision:
        raise RuntimeError("remote DISK snapshot has no revision")
    return revision


@config.command("push")
def push_snapshot(
    service: str,
    directory: Path,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
) -> None:
    from .main import _run, runtime

    async def push() -> dict[str, Any]:
        revision = expected_revision or await _disk_revision(service)
        files = _snapshot_files(directory, runtime().config().snapshot_max_bytes)
        return await request_common(
            service,
            "PushConfigSnapshot",
            {
                "files": files,
                "expected_revision": revision,
            },
        )

    _run(f"{service} PushConfigSnapshot", push())


@config.command("apply")
def apply_snapshot(
    service: str,
    candidate_id: str,
    expected_revision: str | None = typer.Option(None, "--expected-revision"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    from .main import _run
    from .safety import confirm_mutation

    confirm_mutation(
        f"Apply complete {service} snapshot {candidate_id}?",
        yes,
        command="config apply",
    )

    async def apply() -> dict[str, Any]:
        revision = expected_revision or await _disk_revision(service)
        return await request_common(
            service,
            "ApplyConfigSnapshot",
            {
                "candidate_id": candidate_id,
                "expected_revision": revision,
            },
        )

    _run(f"{service} ApplyConfigSnapshot", apply())
