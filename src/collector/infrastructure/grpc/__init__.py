"""Collector v2 gRPC control-plane adapters."""

from .v2_server import CollectorGrpcServerV2, build_v2_grpc_server

__all__ = ["CollectorGrpcServerV2", "build_v2_grpc_server"]
