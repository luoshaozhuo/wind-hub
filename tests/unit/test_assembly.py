"""Unit tests for the composition root (assembly.py).

验证对象：``assemble`` 从配置目录装配出的 :class:`AssembledRuntime`
对象图——协议驱动、sink、处理器链、路由表、调度器各按其配置正确建立。
装配过程纯同步、无网络 I/O，故本测试用临时配置目录即可完整覆盖，且
不依赖任何真实设备或后端。
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path

import yaml

from wind_hub.assembly import assemble, start_runtime, stop_runtime
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus


def _write_yaml(dir_path: Path, name: str, data: dict) -> Path:
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


def _write_minimal_config(base: Path) -> None:
    """写一份最小但完整的配置目录：1 台 Modbus 设备 + 1 个 file sink
    + 1 个点位 + 1 条路由规则 + 1 个 unit_convert 处理器。"""
    _write_yaml(
        base,
        "system.yaml",
        {
            "scheduler": {"default_interval": 1.0, "connect_timeout": 0.2},
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
                    "endpoint": {"host": "10.0.0.1", "port": 502, "extensions": {"unit_id": 1}},
                }
            ],
        },
    )
    _write_yaml(
        base,
        "points.yaml",
        {
            "points": [
                {
                    "point_id": "rotor.speed",
                    "device_id": "d1",
                    "address": {"register_type": "holding", "address": 100},
                    "data_type": "float32",
                }
            ],
        },
    )
    _write_yaml(
        base,
        "routing.yaml",
        {
            "rules": [
                {"name": "default", "match_point_prefix": "rotor.", "targets": ["archive"]},
            ],
        },
    )


def _write_two_device_config(base: Path) -> None:
    """写一份两点位跨两台设备的配置，用于验证按 device_id 分组。"""
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
                    "endpoint": {"host": "10.0.0.1", "port": 502, "extensions": {"unit_id": 1}},
                },
                {
                    "device_id": "d2",
                    "protocol": "modbus",
                    "endpoint": {"host": "10.0.0.2", "port": 502, "extensions": {"unit_id": 2}},
                },
            ],
        },
    )
    _write_yaml(
        base,
        "points.yaml",
        {
            "points": [
                {
                    "point_id": "rotor.speed",
                    "device_id": "d1",
                    "address": {"type": "holding_register", "register": 30001},
                    "data_type": "float32",
                },
                {
                    "point_id": "gen.power",
                    "device_id": "d2",
                    "address": {"type": "holding_register", "register": 30003},
                    "data_type": "float32",
                },
            ],
        },
    )
    _write_yaml(
        base,
        "routing.yaml",
        {
            "rules": [
                {"name": "default", "targets": ["archive"]},
            ],
        },
    )


def test_assemble_builds_runtime_object_graph() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)

        rt = assemble(base)

        assert set(rt.protocols) == {"d1"}
        assert set(rt.sinks) == {"archive"}
        assert rt.pipeline.processor_count == 1
        assert rt.router.table_size == 1
        assert rt.scheduler.device_count == 1
        assert rt.scheduler.sink_count == 1
        assert rt.route_service.explain("d1", "rotor.speed").targets == ["archive"]


def test_assemble_accepts_string_config_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        _write_minimal_config(Path(td))
        rt = assemble(td)
        assert rt.scheduler.device_count == 1


def test_assemble_groups_points_by_device() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_two_device_config(base)

        rt = assemble(base)

        by_device = rt.scheduler._points_by_device  # noqa: SLF001
        assert set(by_device) == {"d1", "d2"}
        assert [p.point_id for p in by_device["d1"]] == ["rotor.speed"]
        assert [p.point_id for p in by_device["d2"]] == ["gen.power"]


# ---------------------------------------------------------------------------
# start_runtime（决策 1）：API 先于调度器启动
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


async def test_start_runtime_starts_api_before_scheduler_finishes() -> None:
    """start_runtime 先拉起 API 服务任务，再启动调度器（决策 1）。

    把 ``scheduler.start`` 替换为「等待 API 就绪事件」的探针：若 API 任务
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

        original_start = rt.scheduler.start

        async def _start_waiting_for_api() -> None:
            await asyncio.wait_for(api_started.wait(), timeout=2.0)

        rt.scheduler.start = _start_waiting_for_api  # type: ignore[method-assign]
        try:
            api_task = await start_runtime(rt, api_server=_FakeServer())  # type: ignore[arg-type]
        finally:
            rt.scheduler.start = original_start  # type: ignore[method-assign]

        assert api_task is not None
        api_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await api_task


async def test_start_runtime_without_api_server_returns_none() -> None:
    """向后兼容：不传 api_server 时返回 None，调度器正常启动。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_minimal_config(base)
        rt = assemble(base, sink_factory=_null_sink_factory)
        try:
            result = await start_runtime(rt)
            assert result is None
            assert rt.scheduler.running is True
        finally:
            await stop_runtime(rt)
        assert rt.scheduler.running is False
