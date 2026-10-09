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
    start_collector,
    start_commander,
    start_server,
)
from tests.support.wait import (
    wait_commander_ready,
    wait_grpc_ready,
    wait_http_ready,
)


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


@pytest.fixture
def collector_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[CollectorProcess]]]:
    """Collector 进程工厂：``await factory(config_dir)`` 返回 gRPC 已就绪的进程。

    测试结束后统一优雅停机并回收日志；启动阶段失败（含 gRPC 不就绪）
    立即强制清理，不残留孤儿进程。
    """
    started: list[CollectorProcess] = []

    async def _start(config_dir: Path, **kwargs: object) -> CollectorProcess:
        proc = start_collector(config_dir, log_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        started.append(proc)
        try:
            await wait_grpc_ready(proc.grpc_target)
        except BaseException:
            proc.kill_tree()
            proc.close_log()
            started.remove(proc)
            raise
        return proc

    yield _start

    for proc in started:
        try:
            if proc.is_running():
                proc.terminate()
        except BaseException:
            proc.kill_tree()
        finally:
            proc.close_log()


@pytest.fixture
def commander_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[CollectorProcess]]]:
    """Commander 进程工厂：``await factory(config_dir)`` 返回 gRPC 已就绪的进程。

    与 ``collector_factory`` 同生命周期语义：测试结束统一优雅停机，
    启动失败立即强制清理。
    """
    started: list[CollectorProcess] = []

    async def _start(config_dir: Path, **kwargs: object) -> CollectorProcess:
        proc = start_commander(config_dir, log_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        started.append(proc)
        try:
            await wait_commander_ready(proc.grpc_target)
        except BaseException:
            proc.kill_tree()
            proc.close_log()
            started.remove(proc)
            raise
        return proc

    yield _start

    for proc in started:
        try:
            if proc.is_running():
                proc.terminate()
        except BaseException:
            proc.kill_tree()
        finally:
            proc.close_log()


SYSTEM_TASK_ID = "modbus-telemetry"
SYSTEM_INSTANCE_ID = "modbus-telemetry:modbus-1"
SYSTEM_COLLECTOR_ID = "collector-1"


@dataclass
class FullStack:
    """完整系统现场：从站 + 两个 Worker + Server + REST 客户端。"""

    modbus: ModbusMockServer
    collector: CollectorProcess
    commander: CollectorProcess
    server: CollectorProcess
    http: httpx.AsyncClient
    config_dir: Path
    sink_path: Path


@pytest.fixture
async def full_stack(
    modbus_server: ModbusMockServer,
    collector_factory,
    commander_factory,
    tmp_path: Path,
) -> AsyncIterator[FullStack]:
    """完整系统现场：真实 Modbus 从站、Collector、Commander 与 Server 进程。

    Server 以缩短的对账/探测周期启动，REST 客户端已确认 HTTP 就绪；
    结束后先关客户端再优雅停机 Server（Worker 由各自工厂回收）。
    """
    sink_path = tmp_path / "out" / "telemetry.jsonl"
    config_dir = write_functional_config(
        tmp_path / "cfg",
        modbus_server.port,
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {
                    "path": str(sink_path),
                    "buffer_size": 4,
                    "flush_interval": 0.5,
                },
            }
        ],
        tasks=[
            {
                "task_id": SYSTEM_TASK_ID,
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )
    collector: CollectorProcess = await collector_factory(
        config_dir, collector_id=SYSTEM_COLLECTOR_ID
    )
    commander: CollectorProcess = await commander_factory(config_dir)

    server = start_server(
        config_dir,
        collectors=[f"{SYSTEM_COLLECTOR_ID}={collector.grpc_target}"],
        commander=commander.grpc_target,
        log_dir=tmp_path,
    )
    try:
        await wait_http_ready(server.grpc_target)
    except BaseException:
        server.kill_tree()
        server.close_log()
        raise

    http = httpx.AsyncClient(
        base_url=f"http://{server.grpc_target}", timeout=10.0
    )
    try:
        yield FullStack(
            modbus=modbus_server,
            collector=collector,
            commander=commander,
            server=server,
            http=http,
            config_dir=config_dir,
            sink_path=sink_path,
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
