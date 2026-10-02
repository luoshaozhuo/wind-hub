from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.application.port.sink import SinkPort
from wind_hub.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub.config.schema import SinkConfig


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
