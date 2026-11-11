"""Register Collector gRPC services at the generated-protobuf adapter boundary."""

from __future__ import annotations

import grpc

from contracts import collector_pb2_grpc, common_pb2_grpc


def register_collector_rpc(
    server: grpc.aio.Server,
    *,
    common: common_pb2_grpc.CommonServiceServicer,
    collector: collector_pb2_grpc.CollectorServiceServicer,
) -> None:
    """Bind both generated service descriptors to the Collector server."""
    common_pb2_grpc.add_CommonServiceServicer_to_server(common, server)
    collector_pb2_grpc.add_CollectorServiceServicer_to_server(collector, server)
