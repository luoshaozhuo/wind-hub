"""Unit tests for the composition root (assembly.py).

验证对象：``assemble`` 从配置目录装配出的 :class:`AssembledRuntime`
对象图——协议驱动、sink、处理器链、采集引擎、Runtime 与 Use Case 各按其
配置正确接线。新模型下 Runtime 直接持有 Task 定义与 ``RuntimeConfig``
（无 scheduler/router/route_query/jobs）。装配过程纯同步、无网络 I/O，
故本测试用临时配置目录即可完整覆盖，且不依赖任何真实设备或后端。
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path

import yaml

from wind_hub.application.runtime import Runtime
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.task import TaskUseCase
from wind_hub.assembly import assemble, start_runtime, stop_runtime
from wind_hub.config.schema import RuntimeConfig, SinkConfig
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus


def _write_yaml(dir_path: Path, name: str, data: dict) -> Path:
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


def _write_minimal_config(base: Path) -> None:
    """写一份最小但完整的配置目录：1 台 Modbus 设备 + 1 个 file sink
    + 1 个点位（point_groups=[fast]）+ 1 个采集 Task + 1 个 unit_convert 处理器。"""
    _write_yaml(
        base,
        "system.yaml",
        {
            "runtime": {"connect_timeout": 0.2},
            "pipeline": {"processors": ["unit_convert"]},
            "sinks": [
                {"name": "archive", "type": "file", "params": {"path": "/tmp/x.csv"}},
            ],
        },
    )
    _write_yaml(
        base,
        "devices.yaml",
        {
            "devices": [
                {
                    "device_id": "d1",
                    "protocol": "modbus",
                    "point_table": "wtg",
                    "endpoint": {"host": "10.0.0.1", "port": 502, "extensions": {"unit_id": 1}},
                }
            ],
        },
    )
    _write_yaml(
        base,
        "points.yaml",
        {
            "point_tables": {
                "wtg": {
                    "points": [
                        {
                            "point_id": "rotor.speed",
                            "point_groups": ["fast"],
                            "address": {"register_type": "holding", "address": 100},
                            "data_type": "float32",
                        }
                    ],
                },
            },
        },
    )
    _write_yaml(
        base,
        "tasks.yaml",
        {
            "tasks": [
                {
                    "task_id": "fast",
                    "device": "d1",
                    "point_group": "fast",
                    "interval": 1.0,
                    "targets": [{"sink": "archive"}],
                },
            ],
        },
    )


def _write_two_device_config(base: Path) -> None:
    """写一份两台设备绑定同一共享点表的配置——用于验证设备无关点表
    经 ``DeviceConfig.point_table`` 绑定后由多设备共享。"""
    _write_yaml(
        base,
        "system.yaml",
        {
            "sinks": [{"name": "archive", "type": "file", "params": {"path": "/tmp/x.csv"}}],
        },
    )
    _write_yaml(
        base,
        "devices.yaml",
        {
            "devices": [
                {
                    "device_id": "d1",
                    "protocol": "modbus",
                    "point_table": "wtg",
                    "device_group": "turbine",
                    "endpoint": {"host": "10.0.0.1", "port": 502, "extensions": {"unit_id": 1}},
                },
                {
                    "device_id": "d2",
                    "protocol": "modbus",
                    "point_table": "wtg",
                    "device_group": "turbine",
                    "endpoint": {"host": "10.0.0.2", "port": 502, "extensions": {"unit_id": 2}},
                },
            ],
        },
    )
    _write_yaml(
        base,
        "points.yaml",
        {
            "point_tables": {
                "wtg": {
                    "points": [
                        {
                            "point_id": "rotor.speed",
                            "point_groups": ["fast"],
                            "address": {"register_type": "holding", "address": 100},
                            "data_type": "float32",
                        },
                        {
                            "point_id": "gen.power",
                            "point_groups": ["fast"],
                            "address": {"register_type": "holding", "address": 102},
                            "data_type": "float32",
                        },
                    ],
                },
            },
        },
    )
    _write_yaml(
        base,
        "tasks.yaml",
        {
            "tasks": [
                {
                    "task_id": "fast",
                    "device_group": "turbine",
                    "point_group": "fast",
                    "interval": 1.0,
                    "targets": [{"sink": "archive"}],
                },
            ],
        },
    )


def test_assemble_builds_runtime_object_graph() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)

        rt = assemble(base)

        assert set(rt.runtime.devices) == {"d1"}
        assert set(rt.sinks) == {"archive"}
        assert rt.pipeline.processor_count == 1
        assert rt.runtime.device_count == 1
        assert rt.runtime.sink_count == 1
        # Task 定义传入 Runtime（实例在 runtime.start() 时展开注册）
        assert set(rt.runtime.task_definitions()) == {"fast"}


def test_assemble_accepts_string_config_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        _write_minimal_config(Path(td))
        rt = assemble(td)
        assert rt.runtime.device_count == 1


def test_assemble_shares_point_table_between_devices() -> None:
    """两台绑定同一 ``point_table`` 的设备共享同一点位表内容。

    点位设备无关：装配按设备绑定解析点表并聚合进各自的运行时
    ``Device``——绑定同一表的设备获得内容一致的独立快照副本（每次
    解析返回新的 list 对象，共享语义体现在引用同一 table_id 且内容
    相等）。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_two_device_config(base)

        rt = assemble(base)

        by_device = {did: dev.points for did, dev in rt.runtime.devices.items()}
        assert set(by_device) == {"d1", "d2"}
        # 同一表被多设备共享——两个设备引用同一 table_id，点表内容一致
        assert by_device["d1"] == by_device["d2"]
        # …但各自持有独立的快照副本（非同一 list 对象）
        assert by_device["d1"] is not by_device["d2"]
        assert [p.point_id for p in by_device["d1"]] == ["rotor.speed", "gen.power"]


def test_assemble_wires_runtime_engine_and_usecases() -> None:
    """新对象图接线：Runtime 持有引擎/Task 定义/分发器，Use Case 委托真实组件。

    Runtime 构造参数为 ``config: RuntimeConfig`` 与 ``tasks``（无 scheduler）。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)

        rt = assemble(base)

        # Runtime 持有 RuntimeConfig（原 SchedulerConfig）与 Task 定义
        assert isinstance(rt.runtime, Runtime)
        assert isinstance(rt.runtime._config, RuntimeConfig)  # noqa: SLF001
        assert rt.runtime._config.connect_timeout == 0.2  # noqa: SLF001
        assert set(rt.runtime._task_defs) == {"fast"}  # noqa: SLF001
        # Runtime 与 AssembledRuntime 暴露的引擎是同一实例
        assert rt.runtime.engine is rt.engine
        # Runtime 与 CommandDispatcher 共享同一 Device 注册表
        # （热重载就地增删后双方立即可见的前提）
        assert rt.dispatcher._devices is rt.runtime.devices  # noqa: SLF001
        # Runtime 持有分发器；Use Case 按职责委托
        assert rt.runtime.dispatcher is rt.dispatcher
        assert isinstance(rt.tasks, TaskUseCase)
        assert rt.tasks._runtime is rt.runtime  # noqa: SLF001
        assert isinstance(rt.config, ConfigUseCase)
        assert rt.query._runtime is rt.runtime  # noqa: SLF001
        assert rt.config._runtime is rt.runtime  # noqa: SLF001


def test_assembled_runtime_has_no_legacy_components() -> None:
    """旧模型组件（scheduler / router / route_query / jobs）已从对象图删除。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)

        rt = assemble(base)

        for attr in ("scheduler", "router", "route_query", "jobs"):
            assert not hasattr(rt, attr), f"legacy attribute still present: {attr}"


# ---------------------------------------------------------------------------
# start_runtime（决策 1）：API 先于 Runtime 启动
# ---------------------------------------------------------------------------


class _NullSink:
    """测试用空 sink——避免 start_runtime 真实打开配置文件里的 file sink。"""

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write(self, batch: list[PointValue]) -> None:
        return None

    async def flush(self) -> None:
        return None

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)


def _null_sink_factory(_cfg: SinkConfig) -> _NullSink:
    return _NullSink()


async def test_start_runtime_starts_api_before_runtime_finishes() -> None:
    """start_runtime 先拉起 API 服务任务，再启动 Runtime（决策 1）。

    把 ``runtime.start`` 替换为「等待 API 就绪事件」的探针：若 API 任务
    没有先行/并发启动，等待必然超时，测试即失败。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)
        rt = assemble(base, sink_factory=_null_sink_factory)

        api_started = asyncio.Event()

        class _FakeServer:
            async def serve(self) -> None:
                api_started.set()

        original_start = rt.runtime.start

        async def _start_waiting_for_api() -> None:
            await asyncio.wait_for(api_started.wait(), timeout=2.0)

        rt.runtime.start = _start_waiting_for_api  # type: ignore[method-assign]
        try:
            api_task = await start_runtime(rt, api_server=_FakeServer())  # type: ignore[arg-type]
        finally:
            rt.runtime.start = original_start  # type: ignore[method-assign]

        assert api_task is not None
        api_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await api_task


async def test_start_runtime_without_api_server_returns_none() -> None:
    """不传 api_server 时返回 None，Runtime 正常启动。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)
        rt = assemble(base, sink_factory=_null_sink_factory)
        try:
            result = await start_runtime(rt)
            assert result is None
            assert rt.runtime.running is True
        finally:
            await stop_runtime(rt)
        assert rt.runtime.running is False
