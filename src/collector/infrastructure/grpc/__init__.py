"""Collector gRPC 入站适配器（控制面 Server；wire contract 来自 shared core）。"""

from .server import CollectorGrpcServer, build_grpc_server

__all__ = ["CollectorGrpcServer", "build_grpc_server"]
