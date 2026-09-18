"""Unit tests for ConfigService hot-reload orchestration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from wind_hub.application.config_service import ConfigService
from wind_hub.config.schema import (
    DeviceConfig,
    PointAddress,
    PointConfig,
    SinkConfig,
)
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.route import RouteRule
from wind_hub.domain.port.outbound import ProtocolPort, SinkPort

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_configs(
    base: Path,
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    points: list[PointConfig] | None = None,
    rules: list[RouteRule] | None = None,
    processors: list[str] | None = None,
) -> None:
    yaml.safe_dump(
        {
            "scheduler": {"queue_maxsize": 10},
            "sinks": [s.model_dump() for s in (sinks or [])],
            "pipeline": {"processors": processors or []},
        },
        (base / "system.yaml").open("w"),
    )
    yaml.safe_dump(
        {"devices": [d.model_dump() for d in (devices or [])]},
        (base / "devices.yaml").open("w"),
    )
    yaml.safe_dump(
        {"points": [p.model_dump() for p in (points or [])]},
        (base / "points.yaml").open("w"),
    )
    yaml.safe_dump(
        {"rules": [r.model_dump() for r in (rules or [])]},
        (base / "routing.yaml").open("w"),
    )


def _make_device(device_id: str, protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        endpoint=Endpoint(host="10.0.0.1", port=502),
    )


def _make_point(device_id: str, point_id: str = "p1") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id=device_id,
        address=PointAddress(type="hr"),
    )


def _mock_protocol_factory() -> MagicMock:
    factory = MagicMock()
    factory.return_value = MagicMock(spec=ProtocolPort)
    factory.return_value.connect = AsyncMock()
    factory.return_value.close = AsyncMock()
    return factory


def _mock_sink_factory() -> MagicMock:
    factory = MagicMock()
    factory.return_value = MagicMock(spec=SinkPort)
    factory.return_value.open = AsyncMock()
    factory.return_value.close = AsyncMock()
    factory.return_value.flush = AsyncMock()
    factory.return_value.write = AsyncMock()
    return factory


def _mock_scheduler() -> MagicMock:
    sched = MagicMock(spec=Scheduler)
    sched.add_device = AsyncMock()
    sched.remove_device = AsyncMock()
    sched.rebuild_device = AsyncMock()
    sched.add_sink = AsyncMock()
    sched.remove_sink = AsyncMock()
    sched.rebuild_sink = AsyncMock()
    sched.replace_router = AsyncMock()
    sched.replace_pipeline = AsyncMock()
    return sched


# ---------------------------------------------------------------------------
# 1. reload() — success, no changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_no_changes() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )
        result = await service.reload()
        assert result.success
        assert not result.diff.has_any_changes


# ---------------------------------------------------------------------------
# 2. reload() — validation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_validation_failure() -> None:
    """If load_config fails during reload, no changes are applied."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # Write valid initial config so __init__ succeeds
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )

        # Corrupt system.yaml to make reload fail
        (base / "system.yaml").write_text("::: invalid yaml :::")
        result = await service.reload()
        assert not result.success
        assert len(result.errors) > 0
        # No scheduler methods were called on failure
        scheduler.add_device.assert_not_called()
        scheduler.remove_device.assert_not_called()


# ---------------------------------------------------------------------------
# 3. reload() — device added
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_device_added() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        protocol_factory = _mock_protocol_factory()
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            protocol_factory,
            _mock_sink_factory(),
        )

        # Modify config to add d2
        _write_configs(
            base,
            devices=[_make_device("d1"), _make_device("d2")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
                PointConfig(
                    point_id="p1",
                    device_id="d2",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        result = await service.reload()
        assert result.success
        assert result.diff.devices.added == ["d2"]
        scheduler.add_device.assert_called_once()


# ---------------------------------------------------------------------------
# 4. reload() — device removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_device_removed() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1"), _make_device("d2")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
                PointConfig(
                    point_id="p1",
                    device_id="d2",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )

        # Remove d2
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        result = await service.reload()
        assert result.success
        assert result.diff.devices.removed == ["d2"]
        scheduler.remove_device.assert_called_with("d2")


# ---------------------------------------------------------------------------
# 5. reload() — device updated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_device_updated() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1", protocol="modbus")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        protocol_factory = _mock_protocol_factory()
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            protocol_factory,
            _mock_sink_factory(),
        )

        # Change protocol
        _write_configs(
            base,
            devices=[_make_device("d1", protocol="iec104")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        result = await service.reload()
        assert result.success
        assert result.diff.devices.updated == ["d1"]
        scheduler.rebuild_device.assert_called_once()


# ---------------------------------------------------------------------------
# 5b. reload() — device added passes correct point table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_device_added_passes_points() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1")],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )

        # Add d2 with its own point; d1's point must NOT leak into d2's table
        d2_point = _make_point("d2")
        _write_configs(
            base,
            devices=[_make_device("d1"), _make_device("d2")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1"), d2_point],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        result = await service.reload()
        assert result.success
        args = scheduler.add_device.call_args
        assert args.args[0] == "d2"
        assert args.args[3] == [d2_point]


# ---------------------------------------------------------------------------
# 5c. reload() — device updated passes correct point table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_device_updated_passes_points() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1", protocol="modbus"), _make_device("d2")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1"), _make_point("d2", point_id="p2")],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )

        # Change d1's protocol; d1 keeps only its own point table
        d1_points = [_make_point("d1")]
        _write_configs(
            base,
            devices=[_make_device("d1", protocol="iec104"), _make_device("d2")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=d1_points + [_make_point("d2", point_id="p2")],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        result = await service.reload()
        assert result.success
        args = scheduler.rebuild_device.call_args
        assert args.args[0] == "d1"
        assert args.args[3] == d1_points


# ---------------------------------------------------------------------------
# 6. reload() — points change → replace_router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_points_change_rebuilds_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )

        # Add a new point
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
                PointConfig(
                    point_id="p2",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        result = await service.reload()
        assert result.success
        assert result.diff.points_changed
        scheduler.replace_router.assert_called_once()


# ---------------------------------------------------------------------------
# 7. reload() — pipeline change → replace_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_pipeline_change_rebuilds_pipeline() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a"],
        )
        scheduler = _mock_scheduler()

        def _proc_factory(name: str, points: list[PointConfig]) -> MagicMock:
            p = MagicMock()
            p.name = name
            return p

        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
            processor_factory=_proc_factory,
        )

        # Change processor list
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a", "b"],
        )
        result = await service.reload()
        assert result.success
        assert result.diff.pipeline_changed
        scheduler.replace_pipeline.assert_called_once()


@pytest.mark.asyncio
async def test_reload_points_change_rebuilds_pipeline() -> None:
    """点表变更（processor 列表未变）也应重建 Pipeline，让 Processor 重新注入新点表。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1", "p1")],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a"],
        )
        scheduler = _mock_scheduler()
        received: list[list[PointConfig]] = []

        def _proc_factory(name: str, points: list[PointConfig]) -> MagicMock:
            received.append(points)
            p = MagicMock()
            p.name = name
            return p

        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
            processor_factory=_proc_factory,
        )

        # 仅变更点表（processor 列表保持 ["a"] 不变）
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1", "p2")],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a"],
        )
        result = await service.reload()
        assert result.success
        assert result.diff.points_changed
        assert not result.diff.pipeline_changed
        scheduler.replace_pipeline.assert_called_once()
        # processor_factory 必须收到新点表（含 p2，不含旧的 p1）
        assert received
        injected_ids = {p.point_id for p in received[-1]}
        assert injected_ids == {"p2"}


# ---------------------------------------------------------------------------
# 8. reload() — returns correct ReloadResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_returns_correct_result() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )
        result = await service.reload()
        assert result.success
        assert result.errors == []
        assert result.duration_ms >= 0
        assert not result.diff.has_any_changes


# ---------------------------------------------------------------------------
# 9. current_config property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_config() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[
                PointConfig(
                    point_id="p1",
                    device_id="d1",
                    address=PointAddress(type="hr"),
                ),
            ],
            rules=[RouteRule(name="default", targets=["s1"])],
        )
        scheduler = _mock_scheduler()
        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
        )
        assert service.current_config.devices.devices[0].device_id == "d1"


# ---------------------------------------------------------------------------
# 10. reload() — pipeline rebuild injects the new point table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_rebuilds_pipeline_with_new_points() -> None:
    """processor_factory 必须收到「新」点表（new_cfg.points.points），而非旧快照。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1")],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a"],
        )
        scheduler = _mock_scheduler()
        received: list[tuple[str, list[PointConfig]]] = []

        def _proc_factory(name: str, points: list[PointConfig]) -> MagicMock:
            received.append((name, points))
            p = MagicMock()
            p.name = name
            return p

        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
            processor_factory=_proc_factory,
        )

        # 变更处理器列表 + 点表（新增一个点）
        new_point = PointConfig(point_id="p2", device_id="d1", address=PointAddress(type="hr"))
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[new_point],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a", "b"],
        )
        result = await service.reload()
        assert result.success
        scheduler.replace_pipeline.assert_called_once()
        assert [name for name, _ in received] == ["a", "b"]
        # 每个处理器都拿到重载后的新点表
        assert all(points == [new_point] for _, points in received)


@pytest.mark.asyncio
async def test_reload_injects_points_into_configurable_processor() -> None:
    """重载后，具备 ``PointsConfigurable`` 能力的处理器收到新的点表配置。

    工厂仿照 ``assembly._create_processor`` 的职责：创建实例后注入点表；
    这里验证 ``set_points_config`` 被以「新点表」调用。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("d1")],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a"],
        )
        scheduler = _mock_scheduler()
        injected: list[list[PointConfig]] = []

        def _proc_factory(name: str, points: list[PointConfig]) -> MagicMock:
            # 仿照 production：创建 → isinstance(PointsConfigurable) → 注入
            p = MagicMock()
            p.name = name
            p.set_points_config = MagicMock()
            p.set_points_config(points)
            injected.append(points)
            return p

        service = ConfigService(
            str(base),
            scheduler,
            _mock_protocol_factory(),
            _mock_sink_factory(),
            processor_factory=_proc_factory,
        )

        new_point = PointConfig(point_id="p2", device_id="d1", address=PointAddress(type="hr"))
        _write_configs(
            base,
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[new_point],
            rules=[RouteRule(name="default", targets=["s1"])],
            processors=["a", "b"],  # 变更处理器列表触发 pipeline 重建
        )
        result = await service.reload()
        assert result.success
        assert result.diff.pipeline_changed
        # 每个被重建的处理器都注入新的点表
        assert injected == [[new_point], [new_point]]
