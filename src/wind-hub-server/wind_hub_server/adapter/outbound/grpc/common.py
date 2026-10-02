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
    """管理异步 gRPC channel，并统一应用 RPC deadline。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be > 0")
        self.target = target
        self.default_timeout = default_timeout
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


    async def call_struct(
        self,
        path: str,
        request: struct_pb2.Struct,
        *,
        timeout: float | None = None,
    ) -> struct_pb2.Struct:
        """调用 Struct → Struct unary RPC，并强制设置 deadline。"""
        call = self.unary_struct(path)
        return await call(
            request,
            timeout=self.default_timeout if timeout is None else timeout,
        )

    async def call_empty_struct(
        self,
        path: str,
        *,
        timeout: float | None = None,
    ) -> struct_pb2.Struct:
        """调用 Empty → Struct unary RPC，并强制设置 deadline。"""
        call = self.unary_empty_struct(path)
        return await call(
            empty_pb2.Empty(),
            timeout=self.default_timeout if timeout is None else timeout,
        )
