"""Gateway gRPC 仅修改反馈数据，不能绕过控制服务。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import grpc
import pytest

from core.infrastructure.rpc.gateway_data import encode_feedback
from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain import PointDefinition, PointRole
from industrial_gateway.infrastructure.grpc.data_server import GatewayDataService


@pytest.mark.asyncio
async def test_data_rpc_accepts_feedback() -> None:
    store = PointStore([PointDefinition("wt1", "active", PointRole.FEEDBACK, writable=False)])
    rpc = GatewayDataService(store)
    response = await rpc.write_many(
        encode_feedback({("wt1", "active"): 500.0}), AsyncMock()
    )
    assert response.fields["updated"].number_value == 1
    assert store.read("wt1", "active").value == 500.0


@pytest.mark.asyncio
async def test_data_rpc_rejects_command() -> None:
    store = PointStore([PointDefinition("wt1", "ctrl", PointRole.COMMAND, writable=True)])
    rpc = GatewayDataService(store)
    context = AsyncMock()
    context.abort.side_effect = RuntimeError("reject")
    with pytest.raises(RuntimeError, match="reject"):
        await rpc.write_many(encode_feedback({("wt1", "ctrl"): 500.0}), context)
    context.abort.assert_awaited_once()
    assert context.abort.call_args.args[0] is grpc.StatusCode.INVALID_ARGUMENT
