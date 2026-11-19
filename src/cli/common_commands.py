"""Shared CommonService RPC helpers for module control and diagnostics."""

from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict, ParseDict


async def request_common(service: str, method: str, values: dict[str, Any]) -> dict[str, Any]:
    """Invoke an authorized CommonService method with bounded deadline."""
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
