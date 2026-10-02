"""Server gRPC client 公共辅助。"""

from __future__ import annotations

from typing import Any

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2


def to_struct(data: dict[str, Any]) -> struct_pb2.Struct:
    """把 JSON 兼容字典转换为 Struct。"""
    message = struct_pb2.Struct()
    json_format.ParseDict(data, message)
    return message


def from_struct(message: struct_pb2.Struct) -> dict[str, Any]:
    """把 Struct 转为普通字典。"""
    return dict(
        json_format.MessageToDict(
            message,
            preserving_proto_field_name=True,
        )
    )


class GrpcClientBase:
    """管理一个异步 gRPC channel。"""

    def __init__(self, target: str) -> None:
        self.target = target
        self._channel = grpc.aio.insecure_channel(target)

    async def close(self) -> None:
        """关闭 channel。"""
        await self._channel.close()

    def unary_struct(self, path: str):
        """构造 Struct → Struct unary RPC callable。"""
        return self._channel.unary_unary(
            path,
            request_serializer=struct_pb2.Struct.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )

    def unary_empty_struct(self, path: str):
        """构造 Empty → Struct unary RPC callable。"""
        return self._channel.unary_unary(
            path,
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )
