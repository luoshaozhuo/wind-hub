"""Bounded, best-effort control submission with per-target FIFO and no retries."""

from __future__ import annotations

from pathlib import Path

import grpc

from industrial_gateway.application.forwarding import (
    CommandForwarder as ApplicationCommandForwarder,
    RecordObserver,
)
from industrial_gateway.infrastructure.adapters.outbound.grpc.commander_client import (
    CommanderRpcClient,
)
from industrial_gateway.infrastructure.config.schema import ControlTarget

# These statuses explicitly reject request execution at the RPC boundary.
# Transport and deadline failures can have executed and must remain UNKNOWN.
_AUTHORITATIVE_REJECTIONS = frozenset(
    {
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.OUT_OF_RANGE,
        grpc.StatusCode.UNIMPLEMENTED,
        grpc.StatusCode.ALREADY_EXISTS,
    }
)


def _definitive_rpc_rejection(error: Exception) -> bool:
    """Recognize explicit RPC rejection without assuming transport settlement."""
    if not isinstance(error, grpc.RpcError):
        return False
    code = getattr(error, "code", None)
    if not callable(code):
        return False
    return code() in _AUTHORITATIVE_REJECTIONS


class CommandForwarder(ApplicationCommandForwarder):
    """Concrete Commander gRPC adapter composition for application forwarding."""

    def __init__(
        self,
        config: ControlTarget,
        *,
        observer: RecordObserver | None = None,
        project_root: Path,
    ) -> None:
        if config.kind != "commander":
            raise ValueError("control forwarding requires Commander")
        super().__init__(
            config,
            observer=observer,
            client=CommanderRpcClient(config, project_root),
            definitive_rejection=_definitive_rpc_rejection,
        )
