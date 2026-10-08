"""Collector gRPC 入站适配器（控制面 Server 与生成的 pb2 代码）。"""

from .server import CollectorGrpcServer, build_grpc_server

__all__ = ["CollectorGrpcServer", "build_grpc_server"]
