"""通用 Gateway 批量遥测 gRPC 客户端，使用标准 Protobuf Struct。

Struct 中的 JSON Number 必须是 IEEE754 精确可表示范围内的整数或有限浮点。
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import grpc
from google.protobuf import json_format, struct_pb2

METHOD = "/wind_hub.gateway.v1.GatewayDataService/WriteMany"
PointKey = tuple[str, str]
Scalar = bool | int | float


def _scalar(value: object) -> Scalar:
    if type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > (1 << 53):
            raise ValueError("integer exceeds exact Struct number range")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("feedback must be finite numeric or bool")


def encode_feedback(values: Mapping[PointKey, Scalar]) -> struct_pb2.Struct:
    if not values:
        raise ValueError("empty feedback batch")
    points = []
    for (device_id, point_id), value in values.items():
        if not device_id or not point_id:
            raise ValueError("feedback point requires device_id and point_id")
        scalar = _scalar(value)
        tagged = (
            {"bool_value": scalar}
            if type(scalar) is bool
            else {"int_value": str(scalar)}
            if type(scalar) is int
            else {"double_value": scalar}
        )
        points.append({"device_id": device_id, "point_id": point_id, "value": tagged})
    result = struct_pb2.Struct()
    result.update({"points": points})
    return result


def decode_feedback(message: struct_pb2.Struct) -> dict[PointKey, Scalar]:
    data = json_format.MessageToDict(message, preserving_proto_field_name=True)
    items = data.get("points")
    if not isinstance(items, list) or not 0 < len(items) <= 5000:
        raise ValueError("feedback batch must contain 1..5000 points")
    result: dict[PointKey, Scalar] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid feedback point")
        if set(item) != {"device_id", "point_id", "value"}:
            raise ValueError("unexpected feedback field")
        device_id, point_id = item["device_id"], item["point_id"]
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("invalid device ID")
        if not isinstance(point_id, str) or not point_id:
            raise ValueError("invalid point ID")
        key = (device_id, point_id)
        if key in result:
            raise ValueError("duplicate feedback point")
        tagged = item["value"]
        if not isinstance(tagged, dict) or len(tagged) != 1:
            raise ValueError("invalid tagged scalar")
        if "int_value" in tagged:
            value = tagged["int_value"]
            if not isinstance(value, str):
                raise ValueError("int_value must be a decimal string")
            numeric = int(value)
        elif "bool_value" in tagged:
            numeric = tagged["bool_value"]
            if type(numeric) is not bool:
                raise ValueError("bool_value must be boolean")
        elif "double_value" in tagged:
            numeric = tagged["double_value"]
            if type(numeric) is not float:
                raise ValueError("double_value must be float")
        else:
            raise ValueError("unknown scalar kind")
        result[key] = _scalar(numeric)
    return result


def create_write_many(
    channel: grpc.aio.Channel,
) -> grpc.aio.UnaryUnaryMultiCallable[struct_pb2.Struct, struct_pb2.Struct]:
    return channel.unary_unary(
        METHOD,
        request_serializer=struct_pb2.Struct.SerializeToString,
        response_deserializer=struct_pb2.Struct.FromString,
    )
