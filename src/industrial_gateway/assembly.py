"""Compose the Industrial Gateway v1 runtime from resolved configuration."""

from __future__ import annotations

from industrial_gateway.infrastructure.config.resolution import ResolvedGateway
from industrial_gateway.infrastructure.runtime import GatewayRuntime


def assemble_gateway(config: ResolvedGateway) -> GatewayRuntime:
    """Build one authoritative variable store and Server per configured device."""
    return GatewayRuntime(config)
