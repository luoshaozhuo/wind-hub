"""Read-only business service configuration snapshot inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from google.protobuf.json_format import MessageToDict, ParseDict

config = typer.Typer(help="Read-only remote configuration snapshots.")


async def request_common(service: str, method: str, values: dict[str, Any]) -> dict[str, Any]:
    """Call the frozen common contract with validated message fields."""
    from google.protobuf import message_factory

    from contracts import common_pb2, common_pb2_grpc

    from .main import runtime

    descriptor = common_pb2.DESCRIPTOR.services_by_name["CommonService"]
    info = descriptor.methods_by_name[method]
    request = ParseDict(values, message_factory.GetMessageClass(info.input_type)())
    stub = common_pb2_grpc.CommonServiceStub(runtime().channel(service))
    metadata = runtime().get_rpc_metadata(service, method)
    response = await getattr(stub, method)(
        request,
        timeout=runtime().target(service).timeout,
        metadata=metadata,
    )
    return MessageToDict(
        response,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )


def _run_common(service: str, method: str, values: dict[str, Any]) -> None:
    from .main import _run

    _run(f"{service} {method}", request_common(service, method, values))


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
