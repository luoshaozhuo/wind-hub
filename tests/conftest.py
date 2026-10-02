"""跨组件共享测试基础 fixture 与 pytest marker 自动分类。

marker 自动分类（``pytest_collection_modifyitems``）按目录/文件名约定打标，
避免为既有测试机械逐文件加 ``pytestmark``：

- 层级：路径含 ``unit`` / ``functional`` / ``integration`` / ``system`` /
  ``recovery`` / ``soak`` 即打对应 marker；``perf`` 只打非 unit 路径下的
  perf 测试（``tests/collector/unit/perf`` 是 perf 框架自身的 unit 测试）；
- 协议：路径或文件名 token 命中 ``modbus`` / ``ads`` / ``iec104`` /
  ``kafka`` / ``postgres``；
- 服务真实性（仅 integration/system/recovery 层级）：默认 ``real_service``
  （真实协议栈 over TCP、真实文件、Docker 服务、真实 PLC）；命中
  :data:`_MOCK_SERVICE_STEMS` 或文件名含 ``mock`` 的打 ``mock_service``
  （monkeypatch / 内存 fake，不计入 real-service 验收）。
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
from wind_hub_collector.config.schema import SinkConfig
from wind_hub_collector.domain.model.errors import ConfigError

FIXTURE_CONFIGS = Path(__file__).resolve().parent / "fixtures" / "configs"
_TESTS_ROOT = Path(__file__).resolve().parent

_LEVEL_MARKERS = ("unit", "functional", "integration", "system", "recovery", "soak")
_PROTOCOL_MARKERS = ("modbus", "ads", "iec104", "kafka", "postgres")
#: 使用 fake/monkeypatch 替代外部组件的 integration 测试（不计入 real-service 验收）。
_MOCK_SERVICE_STEMS = frozenset(
    {
        "test_kafka_sink_e2e",  # AIOKafkaProducer 被内存假生产者替换
        "test_postgres_sink_e2e",  # asyncpg 被内存假模块替换
        "test_collect_route_sink",  # NullSink
        "test_fault_recovery",  # NullSink
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """按目录/文件名约定为测试自动补充层级、协议与服务真实性 marker。"""
    for item in items:
        try:
            parts = item.path.relative_to(_TESTS_ROOT).parts
        except ValueError:
            continue
        part_set = set(parts)
        for level in _LEVEL_MARKERS:
            if level in part_set:
                item.add_marker(getattr(pytest.mark, level))
        if "perf" in part_set and "unit" not in part_set:
            item.add_marker(pytest.mark.perf)
        tokens = set(item.path.stem.split("_")) | part_set
        for protocol in _PROTOCOL_MARKERS:
            if protocol in tokens:
                item.add_marker(getattr(pytest.mark, protocol))
        if part_set & {"integration", "system", "recovery"}:
            if "mock" in tokens or item.path.stem in _MOCK_SERVICE_STEMS:
                item.add_marker(pytest.mark.mock_service)
            else:
                item.add_marker(pytest.mark.real_service)


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
    from tests.env import kafka_bootstrap_from_env
    from tests.fixtures.services import compose

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
    from tests.env import postgres_dsn_from_env
    from tests.fixtures.services import compose

    external = postgres_dsn_from_env()
    if external is not None:
        return external
    request.getfixturevalue("_compose_stack")
    compose.wait_services_healthy(["postgres"])
    return "postgresql://windhub:windhub@127.0.0.1:5432/windhub"
