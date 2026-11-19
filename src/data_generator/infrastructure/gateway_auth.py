"""Authorized plaintext gRPC client channels for trusted internal Gateway endpoints."""

from __future__ import annotations

import grpc

from core.infrastructure.grpc_bearer import bearer_metadata, token_environment


def gateway_channel(endpoint: str, token_ref: str) -> grpc.aio.Channel:
    """Require an explicit token source before connecting on the controlled network."""
    if not endpoint or not token_ref:
        raise ValueError("Gateway endpoint and bearer token source are required")
    token_environment(token_ref)
    bearer_metadata(token_ref)
    return grpc.aio.insecure_channel(endpoint)


def gateway_rpc_metadata(token_ref: str) -> tuple[tuple[str, str], ...]:
    return bearer_metadata(token_ref)
