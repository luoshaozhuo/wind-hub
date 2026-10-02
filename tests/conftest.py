"""跨组件共享测试基础 fixture。"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.sinks.null_sink import NullSink
from wind_hub.application.port.sink import SinkPort
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError

FIXTURE_CONFIGS = Path(__file__).resolve().parent / "fixtures" / "configs"


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

