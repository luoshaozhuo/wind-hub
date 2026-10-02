"""Integration RPC 共享 fixture——真实 Worker subprocess + Server 出站客户端。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import (
    CollectorProcess,
    free_port,
    start_collector,
    start_commander,
)
from tests.support.wait import wait_commander_ready, wait_grpc_ready


@pytest.fixture
async def modbus_server() -> AsyncIterator[ModbusMockServer]:
    """独立空闲端口的真实 Modbus 从站。"""
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
    """Collector 进程工厂（与 tests/system/conftest 同生命周期语义）。"""
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
    """Commander 进程工厂（与 tests/system/conftest 同生命周期语义）。"""
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
