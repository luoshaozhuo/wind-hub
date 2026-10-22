"""Seahorse 数据批量发布；严格使用 Gateway 反馈点映射。"""
from __future__ import annotations

from collections.abc import Mapping

import grpc
from google.protobuf import struct_pb2

from core.infrastructure.rpc.gateway_data import METHOD, encode_feedback
from seahorse.domain.models import FarmSnapshot


class GrpcSnapshotPublisher:
    def __init__(
        self,
        endpoint: str,
        feedback: Mapping[str, frozenset[str]],
        timeout: float = 3.0,
    ) -> None:
        if not (endpoint.startswith("127.0.0.1:") or endpoint.startswith("[::1]:")):
            raise ValueError("gRPC endpoint must be loopback")
        if timeout <= 0 or not feedback:
            raise ValueError("invalid Gateway feedback mapping")
        self._channel = grpc.insecure_channel(endpoint)
        self._write_many = self._channel.unary_unary(
            METHOD,
            request_serializer=struct_pb2.Struct.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )
        self._feedback = feedback
        self._timeout = timeout

    def publish(self, snapshot: FarmSnapshot) -> None:
        devices = {device.device_id: device.points for device in snapshot.devices}
        values = {}
        for device_id, points in self._feedback.items():
            device = devices.get(device_id)
            if device is None:
                raise KeyError(f"unknown Gateway device: {device_id}")
            for point_id in points:
                if point_id not in device:
                    raise KeyError(f"missing Gateway feedback: {device_id}.{point_id}")
                values[(device_id, point_id)] = device[point_id]
        self._write_many(encode_feedback(values), timeout=self._timeout)

    def close(self) -> None:
        self._channel.close()
