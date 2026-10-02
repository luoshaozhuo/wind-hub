"""Commander gRPC 入站适配器。"""

from wind_hub_commander.adapter.inbound.grpc.server import (
    CommanderGrpcServer,
    build_grpc_server,
)

__all__ = ["CommanderGrpcServer", "build_grpc_server"]
