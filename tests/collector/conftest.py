"""tests/collector 共享环境——真实 Modbus fixture + 新 CollectorApp 工厂。

面向需要真实协议 wire（ModbusMockServer）与完整采集链路（assemble_collector）
的组件测试：每次工厂调用独立起一对 fixture server 与 v2 三路径配置树
（service / instances / templates）和 CollectorApp，测试结束统一清理。
Task Instance 默认 STOPPED（模板 desired_state=STOP），测试需显式
:meth:`CollectorAppContext.start_instances` 或把 default_desired_state 置 START。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from collector.assembly import CollectorApp, assemble_collector
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port
from tests.support.v2_config import write_v2_component_tree

#: 组件测试的默认设备/型号/点表 ID（扁平点表规格直接用这套命名）。
DEVICE_ID = "modbus-1"
MODEL_ID = "modbus"
TABLE_ID = "modbus"


@dataclass
class CollectorAppContext:
    """一套运行中的 CollectorApp 与其 v2 配置树、Modbus fixture server。"""

    app: CollectorApp
    tree: Path
    point_table: Path
    server: ModbusMockServer

    async def start_instances(self) -> None:
        """启动全部已注册的采集 Task（v2 每设备一个，键为 device_id）。"""
        for device_id in self.app.runtime.task_instances():
            await self.app.runtime.start_task_instance(device_id)


@pytest.fixture
async def app_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Coroutine[Any, Any, CollectorAppContext]]]:
    """CollectorApp 工厂：每用例一套独立 Modbus 从站与 v2 配置树。

    keyword 透传 :func:`tests.support.v2_config.write_v2_component_tree`
    的点表/设备/任务规格；``point_tables`` 与 write_config_tree 同形。
    缺省现场为单 Modbus 设备 modbus-1（型号/点表 modbus），端点指向
    本次独立启动的 fixture server。
    """
    contexts: list[CollectorAppContext] = []

    async def _factory(
        *,
        point_tables: dict[str, Any] | None = None,
        devices: dict[str, dict[str, Any]] | None = None,
        device_models: dict[str, Any] | None = None,
        default_desired_state: str = "STOP",
        start: bool = False,
        **tree_kwargs: Any,
    ) -> CollectorAppContext:
        port = free_port()
        server = ModbusMockServer(port=port)
        await server.start()
        if point_tables is None:
            raise ValueError("app_factory requires point_tables spec")
        if devices is None:
            devices = {
                DEVICE_ID: {
                    "model": {"id": MODEL_ID, "version": "1.0.0"},
                    "endpoint": {"host": "127.0.0.1", "port": port},
                    "overrides": {"protocol_options": {"slave_id": 1}},
                }
            }
        tree = write_v2_component_tree(
            tmp_path / f"cfg{len(contexts)}",
            devices=devices,
            point_tables=point_tables,
            device_models=device_models,
            default_desired_state=default_desired_state,
            **tree_kwargs,
        )
        app = assemble_collector(
            tree / "services" / "collector.yaml",
            tree / "instances",
            tree / "templates",
            collector_id="collector-component",
        )
        await app.start()
        ctx = CollectorAppContext(
            app=app,
            tree=tree,
            point_table=tree / "templates" / "point_tables" / TABLE_ID / "1.0.0.yaml",
            server=server,
        )
        if start:
            await ctx.start_instances()
        contexts.append(ctx)
        return ctx

    yield _factory

    for ctx in contexts:
        await ctx.app.stop()
        await ctx.server.stop()
