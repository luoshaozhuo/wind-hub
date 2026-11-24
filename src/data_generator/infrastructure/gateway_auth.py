"""Authorized loopback-only Gateway transport for Data Generator clients."""

from __future__ import annotations

import os
import re

import grpc

from data_generator.domain.model import GeneratorError


def gateway_metadata(token_env: str) -> tuple[tuple[str, str], ...]:
    """Resolve one printable secret reference without exposing its value."""
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env) is None:
        raise GeneratorError("invalid Gateway token environment name")
    token = os.environ.get(token_env, "")
    if len(token) < 32 or not token.isascii() or any(not 33 <= ord(ch) <= 126 for ch in token):
        raise GeneratorError("Gateway token is missing or invalid")
    return (("authorization", f"Bearer {token}"),)


def create_gateway_channel(endpoint: str) -> grpc.aio.Channel:
    """Disallow remote plaintext and ambiguous port notation."""
    host, separator, port_text = endpoint.rpartition(":")
    if (
        not separator
        or host not in {"127.0.0.1", "localhost", "::1"}
        or not port_text.isdecimal()
        or not 1 <= int(port_text) <= 65535
    ):
        raise GeneratorError("Gateway endpoint must be loopback host:port")
    return grpc.aio.insecure_channel(endpoint)
