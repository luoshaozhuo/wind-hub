"""端到端测试共享 fixture —— 测试配置、模拟从站、装配与运行时生命周期。"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.sinks.null_sink import NullSink
from wind_hub.application.port.sink import SinkPort
from wind_hub.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError

from .runtime_helpers import clear_runtime_context, set_runtime_context

FIXTURE_CONFIGS = Path(__file__).resolve().parents[2] / "fixtures" / "configs"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """把 fixture 配置目录拷到独立临时目录并返回——测试可自由改写而
    不污染 fixtures/。"""
    dst = tmp_path / "configs"
    shutil.copytree(FIXTURE_CONFIGS, dst)
    return dst


@pytest.fixture
def sink_factory() -> Callable[[SinkConfig], SinkPort]:
    """``null`` sink 工厂，注入 ``assemble(sink_factory=...)``。"""

    def _factory(cfg: SinkConfig) -> SinkPort:
        if cfg.type == "null":
            return NullSink()
        raise ConfigError(f"unknown sink type '{cfg.type}' (test factory)")

    return _factory


@pytest.fixture
async def modbus_server() -> Iterator[ModbusMockServer]:
    server = ModbusMockServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def iec104_server() -> Iterator[IEC104MockServer]:
    server = IEC104MockServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def runtime(
    config_dir: Path,
    sink_factory: Callable[[SinkConfig], SinkPort],
    modbus_server: ModbusMockServer,
    iec104_server: IEC104MockServer,
):
    """装配并启动一个引擎（含 null sink），测试结束后优雅停机。

    依赖两个模拟从站：设备在 ``start_runtime`` 时才真正连接，若从站尚未
    监听，驱动会在 connect 超时后被跳过且不会自动重连，所以必须保证从站
    先起、引擎后起。
    """
    rt = assemble(config_dir, sink_factory=sink_factory)
    await start_runtime(rt)
    # Task Instance 启动后为 STOPPED——显式 start-all 才进入周期采集，
    # 与旧模型的 scheduler 自动调度语义对齐。
    await rt.tasks.start_all_instances()
    try:
        yield rt
    finally:
        await stop_runtime(rt)


@pytest.fixture
async def api_client(runtime: AssembledRuntime):
    """在**同一事件循环**内以 ASGITransport 驱动 FastAPI 应用。

    与 :class:`fastapi.testclient.TestClient` 不同，这里不另起线程/事件循环，
    避免协议驱动（asyncio.Lock、pymodbus 连接都绑定在 runtime 所在循环）在
    跨循环调用 ``read`` / ``write`` 时崩溃。
    """
    from wind_hub.adapter.inbound.webapi.app import build_api

    set_runtime_context(runtime)
    transport = ASGITransport(app=build_api())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    clear_runtime_context()
