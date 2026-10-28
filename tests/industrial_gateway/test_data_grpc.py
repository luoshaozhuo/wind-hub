"""Gateway 强类型 WriteMany：仅写反馈数据，拒绝非法批次与控制写入。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import grpc
import pytest
from google.protobuf import struct_pb2

from contracts import gateway_pb2 as pb
from core.infrastructure.rpc.scalar_codec import encode_scalar
from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain import PointDefinition, PointRole
from industrial_gateway.infrastructure.grpc.data_server import (
    GatewayDataService,
    create_grpc_server,
)

pytestmark = pytest.mark.unit


def _request(points: list[tuple[str, str, object]]) -> pb.WriteManyRequest:
    return pb.WriteManyRequest(
        points=[
            pb.FeedbackPoint(
                device_id=device_id,
                point_id=point_id,
                value=encode_scalar(value),
            )
            for device_id, point_id, value in points
        ]
    )


def _feedback_store() -> PointStore:
    return PointStore(
        [
            PointDefinition("wt1", "active", PointRole.FEEDBACK, writable=False),
            PointDefinition("wt1", "ctrl", PointRole.COMMAND, writable=True),
            PointDefinition("wt1", "p", PointRole.FEEDBACK, writable=False),
            PointDefinition("wt1", "nan", PointRole.FEEDBACK, writable=False),
            PointDefinition("wt1", "inf", PointRole.FEEDBACK, writable=False),
            PointDefinition("wt1", "text", PointRole.FEEDBACK, writable=False),
        ]
        + [PointDefinition("wt1", f"p{i}", PointRole.FEEDBACK, writable=False) for i in range(5001)]
    )


async def _assert_invalid(request: pb.WriteManyRequest, match: str) -> None:
    rpc = GatewayDataService(_feedback_store())
    context = AsyncMock()
    context.abort.side_effect = RuntimeError("reject")
    with pytest.raises(RuntimeError, match="reject"):
        await rpc.WriteMany(request, context)
    context.abort.assert_awaited_once()
    assert context.abort.call_args.args[0] is grpc.StatusCode.INVALID_ARGUMENT
    assert match in context.abort.call_args.args[1]


@pytest.mark.asyncio
async def test_data_rpc_accepts_feedback() -> None:
    store = PointStore([PointDefinition("wt1", "active", PointRole.FEEDBACK, writable=False)])
    rpc = GatewayDataService(store)
    response = await rpc.WriteMany(_request([("wt1", "active", 500.0)]), AsyncMock())
    assert response.updated == 1
    assert store.read("wt1", "active").value == 500.0


@pytest.mark.asyncio
async def test_data_rpc_rejects_command_without_write() -> None:
    store = PointStore([PointDefinition("wt1", "ctrl", PointRole.COMMAND, writable=True)])
    rpc = GatewayDataService(store)
    context = AsyncMock()
    context.abort.side_effect = RuntimeError("reject")
    with pytest.raises(RuntimeError, match="reject"):
        await rpc.WriteMany(_request([("wt1", "ctrl", 500.0)]), context)
    context.abort.assert_awaited_once()
    assert context.abort.call_args.args[0] is grpc.StatusCode.INVALID_ARGUMENT
    # 拒绝路径不得产生写入。
    assert store.read("wt1", "ctrl").value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("points", "match"),
    [
        ([], "1..5000"),
        ([("wt1", f"p{i}", 1.0) for i in range(5001)], "1..5000"),
        ([("", "p", 1.0)], "device_id"),
        ([("  ", "p", 1.0)], "device_id"),
        ([("wt1", " ", 1.0)], "point_id"),
        ([("wt1", "text", "running")], "numeric or bool"),
        ([("wt1", "nan", float("nan"))], "finite"),
        ([("wt1", "inf", float("inf"))], "finite"),
        ([("wt1", "p", 1.0), ("wt1", "p", 2.0)], "duplicate"),
    ],
)
async def test_data_rpc_rejects_invalid_batches(points: list, match: str) -> None:
    await _assert_invalid(_request(points), match)


@pytest.mark.asyncio
async def test_data_rpc_rejects_unset_value() -> None:
    request = pb.WriteManyRequest(points=[pb.FeedbackPoint(device_id="wt1", point_id="p")])
    await _assert_invalid(request, "requires value")


def test_loopback_only_binding() -> None:
    store = PointStore([PointDefinition("wt1", "active", PointRole.FEEDBACK, writable=False)])
    with pytest.raises(ValueError, match="loopback"):
        create_grpc_server(store, "0.0.0.0:50073")


def test_legacy_struct_payload_is_not_accepted() -> None:
    """旧 Struct Gateway 数据协议消息无法冒充强类型 FeedbackPoint。"""
    legacy = struct_pb2.Struct()
    legacy.update({"points": [{"device_id": "wt1", "point_id": "active", "value": 1.0}]})
    with pytest.raises(TypeError):
        pb.WriteManyRequest(points=[legacy])  # type: ignore[list-item]
