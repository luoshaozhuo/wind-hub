"""Collector 最小组合根测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.support.config_helper import write_config_tree
from wind_hub_collector.application.runtime import CollectorRuntime
from wind_hub_collector.application.service.config import CollectorConfigService
from wind_hub_collector.application.service.task import CollectorTaskService
from wind_hub_collector.assembly import CollectorApp, assemble, start_runtime, stop_runtime
from wind_hub_core.config import ResolvedSinkConfig, RuntimeConfig
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue


def _write_minimal_config(base: Path) -> Path:
    """写入 1 Device + 1 Point + 1 Task + 1 Sink 的最小配置。"""
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "d1",
                "protocol": "modbus",
                "point_table": "wtg",
                "endpoint": {
                    "host": "10.0.0.1",
                    "port": 502,
                    "extensions": {"unit_id": 1},
                },
            }
        ],
        point_tables={
            "wtg": {
                "points": [
                    {
                        "point_id": "rotor.speed",
                        "point_groups": ["fast"],
                        "address": {
                            "register_type": "holding",
                            "address": 100,
                        },
                        "data_type": "float32",
                    }
                ],
            },
        },
        sinks=[
            {
                "name": "archive",
                "type": "file",
                "connection": {"path": "/tmp/x.csv", "format": "csv"},
            }
        ],
        tasks=[
            {
                "task_id": "fast",
                "device": "d1",
                "point_group": "fast",
                "interval": 1.0,
                "targets": [{"sink": "archive"}],
            }
        ],
        system={"runtime": {"connect_timeout": 0.2}},
    )


class _NullSink:
    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write(self, batch: list[PointValue]) -> None:
        del batch

    async def flush(self) -> None:
        return None

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)


def _null_sink_factory(_cfg: ResolvedSinkConfig) -> _NullSink:
    return _NullSink()


def test_assemble_builds_minimal_collector_graph() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _write_minimal_config(Path(td))
        assembled = assemble(site)

        assert isinstance(assembled, CollectorApp)
        assert isinstance(assembled.runtime, CollectorRuntime)
        assert isinstance(assembled.tasks, CollectorTaskService)
        assert isinstance(assembled.config, CollectorConfigService)
        assert assembled.runtime.engine is assembled.engine
        assert isinstance(assembled.runtime._config, RuntimeConfig)  # noqa: SLF001
        assert set(assembled.runtime.devices) == {"d1"}
        assert set(assembled.sinks) == {"archive"}
        assert set(assembled.runtime.task_definitions()) == {"fast"}


def test_assembled_runtime_exposes_only_collector_core() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _write_minimal_config(Path(td))
        assembled = assemble(site)

        expected = {
            "boot_config",
            "runtime",
            "engine",
            "sinks",
            "tasks",
            "query",
            "config",
            "metrics_state",
        }
        assert set(assembled.__dataclass_fields__) == expected

        removed = {
            "devices",
            "device_data",
            "device_control",
            "overview",
            "operations",
            "admin_state",
            "config_admin",
            "settings",
            "definitions",
            "sink_ops",
            "diagnostics",
            "monitoring",
            "monitoring_metrics",
            "log_store",
            "quality",
            "logs",
            "system_health",
        }
        for name in removed:
            assert not hasattr(assembled, name)


def test_assemble_loads_config_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import wind_hub_collector.assembly as assembly_module

    real_load = assembly_module.load_config
    calls = 0

    def counting_load(config_dir):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_load(config_dir)

    with tempfile.TemporaryDirectory() as td:
        site = _write_minimal_config(Path(td))
        monkeypatch.setattr(assembly_module, "load_config", counting_load)

        assembled = assemble(site)

        assert calls == 1
        assert assembled.boot_config is not None


async def test_collector_runtime_lifecycle_without_web_components() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _write_minimal_config(Path(td))
        assembled = assemble(site, sink_factory=_null_sink_factory)

        # 避免测试真实连接 Modbus 设备；生命周期本身仍走 CollectorRuntime。
        device = assembled.runtime.devices["d1"]
        original_connect = device.connect

        async def no_connect() -> None:
            return None

        device.connect = no_connect  # type: ignore[method-assign]
        try:
            await start_runtime(assembled)
            assert assembled.runtime.running is True
        finally:
            await stop_runtime(assembled)
            device.connect = original_connect  # type: ignore[method-assign]

        assert assembled.runtime.running is False



def test_create_sink_supports_modbus() -> None:
    import wind_hub_collector.assembly as assembly_module
    from wind_hub_collector.adapter.outbound.sink.modbus import ModbusSink

    cfg = ResolvedSinkConfig(
        name="modbus_scada",
        type="modbus",
        connection={"host": "127.0.0.1", "port": 1502},
        points=[
            {
                "source": {"device_id": "d1", "point_id": "rotor.speed"},
                "ref": "d1.rotor.speed",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {
                    "unit_id": 1,
                    "register_type": "holding",
                    "address": 100,
                },
            }
        ],
    )

    sink = assembly_module._create_sink(cfg)  # noqa: SLF001
    assert isinstance(sink, ModbusSink)
    assert sink.exclusive_open is True
