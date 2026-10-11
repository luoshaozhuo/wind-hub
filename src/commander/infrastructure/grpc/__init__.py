"""Commander gRPC 入站适配器（Server；wire contract 来自 shared core）。"""

from .server import CommanderGrpcServer, build_grpc_server

__all__ = ["CommanderGrpcServer", "build_grpc_server"]
