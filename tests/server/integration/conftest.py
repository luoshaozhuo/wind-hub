"""Server integration fixtures。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.application.port.sink import SinkPort
from wind_hub.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub.config.schema import SinkConfig
from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import AppContext, clear_context, set_context


@pytest.fixture
async def runtime(
    config_dir: Path,
    sink_factory: Callable[[SinkConfig], SinkPort],
    modbus_server: ModbusMockServer,
    iec104_server: IEC104MockServer,
) -> Iterator[AssembledRuntime]:
    rt = assemble(config_dir, sink_factory=sink_factory)
    await start_runtime(rt)
    await rt.tasks.start_all_instances()
    try:
        yield rt
    finally:
        await stop_runtime(rt)


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
