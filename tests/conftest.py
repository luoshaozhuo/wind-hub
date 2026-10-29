"""跨组件共享测试基础 fixture 与 pytest marker 自动补标。

测试按业务归属组织（tests/core、tests/collector、tests/system 等）；
层级（unit/component/contract/integration/system/reliability/performance）
由文件内显式 ``pytestmark`` 或业务包 conftest 声明。本 conftest 只补充
路径可推断的横切 marker：

- tests/system 下未显式声明层级的文件补 ``system``；
- 协议与外部服务：路径或文件名 token 命中 ``modbus`` / ``ads`` /
  ``iec104`` / ``redis`` / ``influxdb`` / ``file``；
- 服务真实性：路径/文件名 token 含 ``mock`` 且层级属于
  integration/system/reliability 时补 ``mock_service``（monkeypatch /
  内存 fake，不计入 real-service 验收）；``real_service`` 从不自动赋值。

更细的环境属性（hardware / docker / root / network / slow / soak）
必须在测试文件或 conftest 中显式标注。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytest_plugins = ("tests.support.worker_fixtures",)

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
_PROTOCOL_MARKERS = ("modbus", "ads", "iec104", "redis", "influxdb", "file")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """为测试补充 system 层级、协议与 mock 真实性 marker。"""
    for item in items:
        try:
            parts = item.path.relative_to(_TESTS_ROOT).parts
        except ValueError:
            continue
        part_set = set(parts)
        existing = {marker.name for marker in item.iter_markers()}
        if parts[0] == "system" and not (set(_LEVEL_MARKERS) & existing):
            item.add_marker(pytest.mark.system)
        tokens = set(item.path.stem.split("_")) | part_set
        for protocol in _PROTOCOL_MARKERS:
            if protocol in tokens:
                item.add_marker(getattr(pytest.mark, protocol))
        if {"integration", "system", "reliability"} & existing and "mock" in tokens:
            item.add_marker(pytest.mark.mock_service)


@pytest.fixture
async def modbus_server() -> AsyncGenerator[ModbusMockServer, None]:
    server = ModbusMockServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def iec104_server() -> AsyncGenerator[IEC104MockServer, None]:
    server = IEC104MockServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# 真实外部服务（Redis）——session 级，环境变量优先，否则本机
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
def redis_service(request: pytest.FixtureRequest) -> str:
    """真实 Redis 地址（如 ``127.0.0.1:6379``）。

    ``WIND_HUB_TEST_REDIS`` 指向外部实例时直接使用且不管理其生命周期。
    """
    from tests.fixtures.services import compose
    from tests.support.env import redis_address_from_env

    external = redis_address_from_env()
    if external is not None:
        return external
    request.getfixturevalue("_compose_stack")
    compose.wait_services_healthy(["redis"])
    return "127.0.0.1:6379"


@pytest.fixture(scope="session")
async def nats_service() -> str:
    """真实 NATS 地址（默认 ``nats://127.0.0.1:4222``）。

    ``WIND_HUB_TEST_NATS`` 指向外部实例时使用该地址；NATS 不纳入 Redis
    Compose 栈，需由测试环境显式提供（JetStream 在需要 reliable 时另开
    ``-js``）。不可达时明确 skip——绝不退化为内存替身冒充真实链路。
    """
    import os

    from tests.support.wait import WaitTimeoutError, wait_until

    servers = os.environ.get("WIND_HUB_TEST_NATS", "nats://127.0.0.1:4222")
    host_port = servers.removeprefix("nats://").partition("@")[2] or servers.removeprefix("nats://")
    host, _, port = host_port.partition(":")

    async def _reachable() -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host or "127.0.0.1", int(port or "4222")),
                timeout=1.0,
            )
        except OSError:
            return None
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    try:
        reachable = await wait_until(_reachable, timeout=10.0, interval=0.5)
    except WaitTimeoutError:
        reachable = False
    if not reachable:
        pytest.skip(
            f"SKIPPED: real NATS not reachable at {servers} "
            "(set WIND_HUB_TEST_NATS or start a nats server)"
        )
    return servers
