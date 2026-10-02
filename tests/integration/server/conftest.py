"""Server integration fixtures。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from wind_hub.assembly import AssembledRuntime
from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import AppContext, clear_context, set_context


@pytest.fixture
async def api_client(runtime: AssembledRuntime) -> AsyncIterator[AsyncClient]:
    set_context(
        AppContext(
            config=runtime.config,
            tasks=runtime.tasks,
            runtime=runtime.runtime,
            command=runtime.command,
            query=runtime.query,
        )
    )
    transport = ASGITransport(app=build_api())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        clear_context()
