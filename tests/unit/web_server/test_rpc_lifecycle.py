"""RPC gateway 生命周期和失败清理测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from web_server.rpc import RpcGateway, RpcSettings


@pytest.mark.asyncio
async def test_rpc_channels_closed_on_normal_exit() -> None:
    first = AsyncMock()
    second = AsyncMock()
    with patch("web_server.rpc.grpc.aio.insecure_channel", side_effect=[first, second]):
        async with RpcGateway(RpcSettings()):
            pass

    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_channel_closed_if_second_channel_creation_fails() -> None:
    first = AsyncMock()
    with patch(
        "web_server.rpc.grpc.aio.insecure_channel",
        side_effect=[first, RuntimeError("commander channel failed")],
    ):
        with pytest.raises(RuntimeError, match="commander channel failed"):
            async with RpcGateway(RpcSettings()):
                pass

    first.close.assert_awaited_once()
