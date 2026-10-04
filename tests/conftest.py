"""跨组件共享测试基础 fixture 与 pytest marker 自动分类。

marker 自动分类（``pytest_collection_modifyitems``）按目录/文件名约定打标，
避免为既有测试机械逐文件加 ``pytestmark``：

- 层级：第一层目录即层级（``unit`` / ``component`` / ``contract`` /
  ``integration`` / ``system`` / ``reliability`` / ``performance``）；
  ``reliability/soak`` 额外打 ``soak``；
- 协议与外部服务：路径或文件名 token 命中 ``modbus`` / ``ads`` /
  ``iec104`` / ``kafka`` / ``postgres`` / ``influxdb`` / ``file``；
- 服务真实性（仅 integration/system/reliability 层级）：路径/文件名
  token 含 ``mock`` 的打 ``mock_service``（monkeypatch / 内存 fake，
  不计入 real-service 验收）；``real_service`` **不按目录默认赋值**——
  真实服务测试必须在文件内显式标注（模块级 ``pytestmark``），避免
  文件放错目录即被误认证为真实服务验收。

层级 marker 只靠第一层目录推断；更细的环境属性（hardware / docker /
root / network / slow）必须在测试文件或 conftest 中显式标注。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.sinks.null_sink import NullSink
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.errors import ConfigError

FIXTURE_CONFIGS = Path(__file__).resolve().parent / "fixtures" / "configs"
_TESTS_ROOT = Path(__file__).resolve().parent

_LEVEL_MARKERS = (
    "unit",
    "component",
    "contract",
    "integration",
    "system",
    "reliability",
    "performance",
)
_PROTOCOL_MARKERS = ("modbus", "ads", "iec104", "kafka", "postgres", "influxdb", "file")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """按目录/文件名约定为测试自动补充层级、协议与服务真实性 marker。"""
    for item in items:
        try:
            parts = item.path.relative_to(_TESTS_ROOT).parts
        except ValueError:
            continue
        part_set = set(parts)
        level = parts[0] if parts else ""
        if level in _LEVEL_MARKERS:
            item.add_marker(getattr(pytest.mark, level))
        if level == "reliability" and "soak" in part_set:
            item.add_marker(pytest.mark.soak)
        tokens = set(item.path.stem.split("_")) | part_set
        for protocol in _PROTOCOL_MARKERS:
            if protocol in tokens:
                item.add_marker(getattr(pytest.mark, protocol))
        if level in {"integration", "system", "reliability"} and "mock" in tokens:
            item.add_marker(pytest.mark.mock_service)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """把 fixture 配置目录拷到独立临时目录并返回——测试可自由改写而
    不污染 fixtures/。"""
    dst = tmp_path / "configs"
    shutil.copytree(FIXTURE_CONFIGS, dst)
    return dst


@pytest.fixture
def sink_factory() -> Callable[[ResolvedSinkConfig], SinkPort]:
    """``null`` sink 工厂，注入 ``assemble(sink_factory=...)``。"""

    def _factory(cfg: ResolvedSinkConfig) -> SinkPort:
        if cfg.name == "null_sink":
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


# ---------------------------------------------------------------------------
# 真实外部服务（Kafka / PostgreSQL）——session 级，环境变量优先，否则本机
# Docker Compose；两者都不可用时明确 skip，绝不退化为 mock。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _compose_stack() -> Iterator[None]:
    """本机 Docker Compose 测试服务栈（按需启动，session 结束销毁）。"""
    from tests.fixtures.services import compose

    if not compose.docker_available():
        pytest.skip(
            "SKIPPED: real service environment not configured "
            "(env vars unset and docker unavailable)"
        )
    compose.compose_up()
    try:
        yield
    finally:
        compose.compose_down()


@pytest.fixture(scope="session")
def kafka_service(request: pytest.FixtureRequest) -> str:
    """真实 Kafka bootstrap servers（如 ``127.0.0.1:9092``）。

    ``WIND_HUB_TEST_KAFKA`` 指向外部实例时直接使用且不管理其生命周期。
    """
    from tests.fixtures.services import compose
    from tests.support.env import kafka_bootstrap_from_env

    external = kafka_bootstrap_from_env()
    if external is not None:
        return external
    request.getfixturevalue("_compose_stack")
    compose.wait_services_healthy(["kafka"])
    return "127.0.0.1:9092"


@pytest.fixture(scope="session")
def postgres_service(request: pytest.FixtureRequest) -> str:
    """真实 PostgreSQL DSN。

    ``WIND_HUB_TEST_POSTGRES_DSN`` 指向外部实例时直接使用且不管理其生命周期。
    """
    from tests.fixtures.services import compose
    from tests.support.env import postgres_dsn_from_env

    external = postgres_dsn_from_env()
    if external is not None:
        return external
    request.getfixturevalue("_compose_stack")
    compose.wait_services_healthy(["postgres"])
    return "postgresql://windhub:windhub@127.0.0.1:5432/windhub"
