"""System E2E 共享 fixture——把 subprocess harness 组装成测试可直接消费的形态。

约定：

- 每个测试的 Collector 使用独立配置目录（``tmp_path``）与独立 gRPC 端口，
  互不共享进程与状态；
- fixture 结束一律走 :meth:`CollectorProcess.terminate` 优雅停机；
  已异常退出的进程跳过 terminate，只回收日志；
- 协议 server fixture 复用 ``tests/fixtures/servers`` 的真实协议栈实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.functional_config import write_functional_config
from tests.support.process import (
    CollectorProcess,
    free_port,
    start_web_server,
)
from tests.support.wait import wait_until

#: full_stack 现场的稳定身份：collector_id 由本 fixture 显式指定，
#: task id 来自 write_functional_config 的默认任务（见 functional_config）。
SYSTEM_COLLECTOR_ID = "collector"
SYSTEM_TASK_ID = "modbus-telemetry"


@pytest.fixture
async def modbus_server() -> AsyncIterator[ModbusMockServer]:
    """独立空闲端口的真实 Modbus 从站（覆盖根 conftest 的固定端口版本）。

    System 测试的 Collector 是独立 subprocess，配置必须引用实际端口；
    每测试一个空闲端口，避免与并行的其他套件争用固定端口。
    """
    server = ModbusMockServer(port=free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@dataclass
class FullStack:
    """完整系统现场：Modbus 从站 + Collector + Commander + Web Server + REST 客户端。"""

    modbus: ModbusMockServer
    collector: CollectorProcess
    commander: CollectorProcess
    server: CollectorProcess
    http: httpx.AsyncClient
    config_dir: Path


@pytest.fixture
async def full_stack(
    modbus_server: ModbusMockServer,
    collector_factory: Callable[..., Awaitable[CollectorProcess]],
    commander_factory: Callable[..., Awaitable[CollectorProcess]],
    tmp_path: Path,
) -> AsyncIterator[FullStack]:
    """REST → Server → Commander → Modbus 全链路真实进程现场。

    Server 以独立 HTTP 端口启动，REST 客户端已确认就绪；结束后先关
    客户端再优雅停机 Server（Worker 由各自工厂回收）。
    """
    config_dir = write_functional_config(tmp_path / "cfg", modbus_server.port)
    collector = await collector_factory(config_dir, collector_id="collector")
    commander = await commander_factory(config_dir)

    http_port = free_port()
    server = start_web_server(
        config_dir,
        http_port=http_port,
        collectors=(f"collector={collector.grpc_target}",),
        commander=commander.grpc_target,
        log_dir=tmp_path,
    )
    url = f"http://127.0.0.1:{http_port}"

    async def _http_ready() -> bool | None:
        try:
            async with httpx.AsyncClient(base_url=url, timeout=2.0) as probe:
                return (await probe.get("/openapi.json")).status_code == 200 or None
        except httpx.HTTPError:
            return None

    try:
        await wait_until(_http_ready, timeout=30.0, interval=0.3)
    except BaseException:
        server.kill_tree()
        server.close_log()
        raise

    http = httpx.AsyncClient(base_url=url, timeout=10.0)
    try:
        yield FullStack(
            modbus=modbus_server,
            collector=collector,
            commander=commander,
            server=server,
            http=http,
            config_dir=config_dir,
        )
    finally:
        await http.aclose()
        try:
            if server.is_running():
                server.terminate()
        except BaseException:
            server.kill_tree()
        finally:
            server.close_log()
