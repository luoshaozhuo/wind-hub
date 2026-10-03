"""Collector functional 测试共享装配。

每个测试在独立临时目录写出自包含配置（真实 Modbus TCP fixture server、
独立端口），经 ``assemble`` + ``start_runtime`` 启动真实 Runtime——
不 monkeypatch 任何被测组件。sink 默认注入 ``NullSink``（类型 ``null``），
其余类型（file/kafka/db）委托组合根的真实工厂，保证 functional 层可以
同时使用真实 FileSink。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.sinks.null_sink import NullSink
from tests.support.config_helper import write_config_tree
from tests.support.process import free_port
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub_core.config.sinks import SinkConfig

#: 与 ModbusMockServer 默认寄存器布局一致的点表。
MODBUS_POINTS: list[dict[str, Any]] = [
    {
        "point_id": "rotor.speed",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 100},
        "data_type": "float32",
    },
    {
        "point_id": "gen.power",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 102},
        "data_type": "float32",
        # 工程值换算验证点：raw 800.0 → 800*2+10 = 1610.0。
        "scale": 2.0,
        "offset": 10.0,
    },
    {
        "point_id": "temp.int",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 104},
        "data_type": "int16",
    },
    {
        "point_id": "setpoint.power",
        "point_groups": ["telemetry", "control"],
        "address": {"register_type": "holding", "address": 200},
        "data_type": "float32",
    },
]

DEFAULT_TASK: dict[str, Any] = {
    "task_id": "modbus-telemetry",
    "device": "modbus-1",
    "point_group": "telemetry",
    "interval": 0.2,
    "targets": [{"sink": "null_sink"}],
}


def functional_sink_factory(cfg: SinkConfig) -> SinkPort:
    """``null`` 注入 NullSink，其余类型走组合根真实工厂（file/kafka/db）。"""
    if cfg.name == "null_sink":
        return NullSink()
    # 复用组合根的适配器装配，避免在测试侧复制第二套 sink 接线。
    from wind_hub_collector.assembly import _create_sink

    return _create_sink(cfg)


def modbus_device_dict(port: int, **overrides: Any) -> dict[str, Any]:
    """指向指定端口 Modbus fixture 的设备定义（旧式便捷写法）。"""
    device: dict[str, Any] = {
        "device_id": "modbus-1",
        "protocol": "modbus",
        "point_table": "modbus",
        "endpoint": {
            "host": "127.0.0.1",
            "port": port,
            "extensions": {
                "unit_id": 1,
                "timeout": 2.0,
                "reconnect_max_retries": 20,
                "reconnect_backoff_max": 1.0,
                # fixture server 寄存器布局为 big-endian float32。
                "word_order": "big_endian",
            },
        },
    }
    device.update(overrides)
    return device


#: devices 参数允许传 callable：在工厂选定实际端口后再构造设备列表。
DevicesArg = list[dict[str, Any]] | Callable[[int], list[dict[str, Any]]]


def write_functional_config(
    base: Path,
    port: int,
    *,
    devices: DevicesArg | None = None,
    point_tables: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    sinks: list[dict[str, Any]] | None = None,
    system: dict[str, Any] | None = None,
    device_models: dict[str, Any] | None = None,
) -> Path:
    """写出 functional 测试默认配置树（单 Modbus 设备 + null sink）。"""
    if callable(devices):
        devices = devices(port)
    return write_config_tree(
        base,
        devices=devices if devices is not None else [modbus_device_dict(port)],
        point_tables=point_tables or {"modbus": {"points": list(MODBUS_POINTS)}},
        device_models=device_models,
        sinks=sinks
        if sinks is not None
        else [{"name": "null_sink", "type": "file", "connection": {"path": "/tmp/wind-hub-null.jsonl"}}],
        tasks=tasks if tasks is not None else [dict(DEFAULT_TASK)],
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
                **((system or {}).get("runtime") or {}),
            },
            **{k: v for k, v in (system or {}).items() if k != "runtime"},
        },
    )


def update_yaml(config_dir: Path, name: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """读-改-写配置目录中的单个 YAML 文件（reload 测试用）。"""
    path = config_dir / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


@dataclass
class FunctionalContext:
    """一个已启动的 functional 运行时及其依赖。"""

    rt: AssembledRuntime
    config_dir: Path
    server: ModbusMockServer | None
    port: int


@pytest.fixture
async def runtime_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., AsyncIterator[FunctionalContext]]]:
    """运行时工厂：``async with factory(...) as ctx`` 获得已启动运行时。

    每个上下文使用独立 Modbus fixture server（空闲端口）与独立配置目录；
    退出时先停 Runtime 再停 server。``with_server=False`` 时不启动协议
    server（连接失败语义测试用）。
    """
    contexts: list[FunctionalContext] = []

    class _Factory:
        def __init__(self, config_dir: Path, server: ModbusMockServer | None, port: int) -> None:
            self._config_dir = config_dir
            self._server = server
            self._port = port

        async def __aenter__(self) -> FunctionalContext:
            if self._server is not None:
                await self._server.start()
            rt = assemble(self._config_dir, sink_factory=functional_sink_factory)
            await start_runtime(rt)
            ctx = FunctionalContext(
                rt=rt,
                config_dir=self._config_dir,
                server=self._server,
                port=self._port,
            )
            contexts.append(ctx)
            return ctx

        async def __aexit__(self, *exc: object) -> None:
            pass

    def _factory(
        *,
        port: int | None = None,
        with_server: bool = True,
        server: ModbusMockServer | None = None,
        subdir: str = "cfg",
        **config_kwargs: Any,
    ) -> _Factory:
        actual_port = port if port is not None else free_port()
        actual_server = server
        if actual_server is None and with_server:
            actual_server = ModbusMockServer(port=actual_port)
        config_dir = write_functional_config(
            tmp_path / subdir, actual_port, **config_kwargs
        )
        return _Factory(config_dir, actual_server, actual_port)

    yield _factory

    for ctx in reversed(contexts):
        try:
            await stop_runtime(ctx.rt)
        finally:
            if ctx.server is not None:
                await ctx.server.stop()


@pytest.fixture
async def modbus_runtime(
    runtime_factory: Callable[..., Any],
) -> AsyncIterator[FunctionalContext]:
    """默认配置（单设备 + telemetry task + null sink）的已启动运行时。"""
    async with runtime_factory() as ctx:
        yield ctx


@pytest.fixture
async def runtime(
    config_dir: Path,
    sink_factory: Callable[[SinkConfig], SinkPort],
    modbus_server: ModbusMockServer,
    iec104_server: IEC104MockServer,
) -> AsyncIterator[AssembledRuntime]:
    """基于共享 fixture 配置集（Modbus + IEC104 设备）的已启动运行时。

    供 collect→route→sink 全链路与故障恢复测试使用；所有 Task Instance
    在交付前已启动。
    """
    rt = assemble(config_dir, sink_factory=sink_factory)
    await start_runtime(rt)
    await rt.tasks.start_all_instances()
    try:
        yield rt
    finally:
        await stop_runtime(rt)
