"""Shared Gateway client transport security policy for production RPC and diagnostics."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import grpc

from data_generator.domain.model import GeneratorError


def gateway_credentials(gateway_tls: dict[str, str] | None) -> grpc.ChannelCredentials | None:
    """Load explicit client identity; absent TLS only permits numeric loopback."""
    if gateway_tls is None:
        return None
    expected = {"cert_file", "key_file", "ca_file"}
    if set(gateway_tls) != expected:
        raise ValueError("gateway_tls requires cert_file, key_file and ca_file")
    if any(
        not isinstance(value, str) or not Path(value).is_absolute()
        for value in gateway_tls.values()
    ):
        raise ValueError("gateway_tls certificate paths must be absolute")
    return grpc.ssl_channel_credentials(
        root_certificates=Path(gateway_tls["ca_file"]).read_bytes(),
        private_key=Path(gateway_tls["key_file"]).read_bytes(),
        certificate_chain=Path(gateway_tls["cert_file"]).read_bytes(),
    )


def create_gateway_channel(
    endpoint: str, credentials: grpc.ChannelCredentials | None
) -> grpc.aio.Channel:
    """Never silently downgrade a non-loopback endpoint to plaintext."""
    if credentials is not None:
        return grpc.aio.secure_channel(endpoint, credentials)
    hostname = endpoint.rsplit(":", 1)[0].strip("[]")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise GeneratorError("Gateway mTLS is required for non-loopback endpoints")
    return grpc.aio.insecure_channel(endpoint)
