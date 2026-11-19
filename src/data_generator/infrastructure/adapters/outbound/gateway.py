"""Gateway gRPC output composition; all lease and retry policy is in Application."""

from __future__ import annotations

from data_generator.application.output_coordinator import GatewayOutputCoordinator
from data_generator.infrastructure.adapters.outbound.gateway_rpc import GatewayRpcClient


class GatewayWriter(GatewayOutputCoordinator):
    """Bind the production Gateway gRPC transport to Application output policy."""

    def __init__(
        self,
        timeout: float = 3.0,
        max_points: int = 1000,
        max_inflight: int = 1,
        gateway_token_ref: str,
    ) -> None:
        super().__init__(
            GatewayRpcClient(gateway_token_ref),
            timeout=timeout,
            max_points=max_points,
            max_inflight=max_inflight,
        )
