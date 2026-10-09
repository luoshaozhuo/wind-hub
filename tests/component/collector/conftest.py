"""tests/component/collector 共享环境——真实 Modbus fixture + 新 CollectorApp 工厂。

面向需要真实协议 wire（ModbusMockServer）与完整采集链路（assemble_collector）
的组件测试：每个工厂调用独立起一对 fixture server 与 CollectorApp，测试结束
统一清理。Task Instance 默认 STOPPED，测试需显式 ``start_instances()``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from collector.assembly import CollectorApp, assemble_collector
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.functional_config import write_functional_config
from tests.support.process import free_port


@dataclass
class CollectorAppContext:
    """一套运行中的 CollectorApp 与其配置目录、Modbus fixture server。"""

    app: CollectorApp
    config_dir: Path
    server: ModbusMockServer

    async def start_instances(self) -> None:
        """启动全部已注册的采集 Task Instance（程序启动不自动开始采集）。"""
        for instance_id in self.app.runtime.task_instances():
            await self.app.runtime.start_task_instance(instance_id)


@pytest.fixture
async def app_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., AsyncIterator[CollectorAppContext]]]:
    """CollectorApp 工厂：每个用例可装配一套独立环境，keyword 透传 write_functional_config。"""
    contexts: list[CollectorAppContext] = []

    async def _factory(**overrides: Any) -> CollectorAppContext:
        port = free_port()
        server = ModbusMockServer(port=port)
        await server.start()
        config_dir = write_functional_config(
            tmp_path / f"cfg{len(contexts)}", port, **overrides
        )
        app = assemble_collector(config_dir, collector_id="collector-component")
        await app.start()
        ctx = CollectorAppContext(app=app, config_dir=config_dir, server=server)
        contexts.append(ctx)
        return ctx

    yield _factory

    for ctx in contexts:
        await ctx.app.stop()
        await ctx.server.stop()
