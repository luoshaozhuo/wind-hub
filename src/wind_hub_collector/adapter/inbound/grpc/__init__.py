"""Collector gRPC 入站控制面适配器；只暴露低频查询与控制 RPC。"""

from .server import CollectorGrpcServer, build_grpc_server

__all__ = ["CollectorGrpcServer", "build_grpc_server"]
