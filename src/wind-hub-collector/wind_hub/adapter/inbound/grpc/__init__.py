"""Collector gRPC inbound adapter。"""

from .server import CollectorGrpcServer, build_grpc_server

__all__ = ["CollectorGrpcServer", "build_grpc_server"]
