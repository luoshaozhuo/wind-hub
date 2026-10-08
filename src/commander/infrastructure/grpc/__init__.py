"""Commander gRPC 入站适配器（Server 与生成的 pb2 代码）。"""

from .server import CommanderGrpcServer, build_grpc_server

__all__ = ["CommanderGrpcServer", "build_grpc_server"]
