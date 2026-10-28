"""Seahorse 数据批量发布；严格使用 Gateway 反馈点映射。"""

from __future__ import annotations

from collections.abc import Mapping

import grpc

from contracts import gateway_pb2 as pb
from contracts import gateway_pb2_grpc as rpc
from core.infrastructure.rpc.scalar_codec import encode_scalar
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
        self._stub = rpc.GatewayDataServiceStub(self._channel)
        self._feedback = feedback
        self._timeout = timeout

    def publish(self, snapshot: FarmSnapshot) -> None:
        devices = {device.device_id: device.points for device in snapshot.devices}
        points: list[pb.FeedbackPoint] = []
        for device_id, names in self._feedback.items():
            device = devices.get(device_id)
            if device is None:
                raise KeyError(f"unknown Gateway device: {device_id}")
            for point_id in names:
                if point_id not in device:
                    raise KeyError(f"missing Gateway feedback: {device_id}.{point_id}")
                points.append(
                    pb.FeedbackPoint(
                        device_id=device_id,
                        point_id=point_id,
                        value=encode_scalar(device[point_id]),
                    )
                )
        self._stub.WriteMany(pb.WriteManyRequest(points=points), timeout=self._timeout)

    def close(self) -> None:
        self._channel.close()
