from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import CollectorRuntime
from wind_hub_collector.application.runtime.device import CollectorDeviceSession
from wind_hub_collector.application.usecase.query import QueryUseCase
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config.schema import (
    CollectionTaskConfig,
    DeviceConfig,
    PointAddress,
    PointConfig,
    RuntimeConfig,
    TaskTarget,
)
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.protocol.port import ProtocolPort

pytestmark = pytest.mark.asyncio


def _device(device_id: str = "d1", protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        point_table="t1",
        endpoint=Endpoint(host="h", port=1),
    )


def _point(point_id: str = "p1") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        point_groups=["g"],
        address=PointAddress(type="hr"),
    )


def _task(task_id: str = "task-1", device: str = "d1") -> CollectionTaskConfig:
    return CollectionTaskConfig(
        task_id=task_id,
        device=device,
        point_group="g",
        interval=1.0,
        targets=[TaskTarget(sink="s1")],
    )


def _protocol(healthy: bool = True) -> MagicMock:
    proto = MagicMock(spec=ProtocolPort)
    proto.health = MagicMock(return_value=HealthStatus(healthy=healthy))
    proto.connect = AsyncMock()
    proto.close = AsyncMock()
    return proto


def _sink(healthy: bool = True) -> MagicMock:
    sink = MagicMock(spec=SinkPort)
    sink.health = MagicMock(return_value=HealthStatus(healthy=healthy))
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.flush = AsyncMock()
    return sink


def _runtime(
    devices: dict[str, DeviceConfig] | None = None,
    protocols: dict[str, MagicMock] | None = None,
    sinks: dict[str, MagicMock] | None = None,
    points: dict[str, list[PointConfig]] | None = None,
    tasks: dict[str, CollectionTaskConfig] | None = None,
) -> CollectorRuntime:
    devices = devices or {}
    protocols = protocols or {}
    points = points or {}
    device_map = {
        device_id: CollectorDeviceSession(
            cfg,
            points.get(device_id, []),
            protocols.get(device_id) or _protocol(),
        )
        for device_id, cfg in devices.items()
    }
    return CollectorRuntime(
        devices=device_map,
        sinks=sinks or {},
        engine=AcquisitionEngine(),
        config=RuntimeConfig(),
        tasks=tasks,
    )


async def test_list_devices_reports_current_runtime_health() -> None:
    usecase = QueryUseCase(
        _runtime(
            devices={"d1": _device("d1", "modbus"), "d2": _device("d2", "iec104")},
            protocols={"d1": _protocol(True), "d2": _protocol(False)},
        )
    )

    infos = await usecase.list_devices()

    by_id = {item.device_id: item for item in infos}
    assert set(by_id) == {"d1", "d2"}
    assert by_id["d1"].connected is True
    assert by_id["d1"].protocol == "modbus"
    assert by_id["d2"].connected is False


async def test_status_aggregates_runtime_snapshot() -> None:
    usecase = QueryUseCase(
        _runtime(
            devices={"d1": _device("d1"), "d2": _device("d2")},
            protocols={"d1": _protocol(True), "d2": _protocol(False)},
            sinks={"s1": _sink(True), "s2": _sink(False)},
        )
    )

    status = await usecase.status()

    assert status.running is False
    assert status.device_count == 2
    assert status.sink_count == 2
    assert status.devices_connected == 1
    assert status.sinks_healthy == 1
    assert status.points_collected == 0
    assert status.points_routed == 0
    assert status.points_dropped == 0
    assert status.acquisitions == []


async def test_status_reports_acquisition_execution_state() -> None:
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": _protocol()},
        points={"d1": [_point()]},
        tasks={"task-1": _task()},
    )
    await runtime.start()
    usecase = QueryUseCase(runtime)

    runtime.report_collect_started("task-1:d1", "d1", "g")
    running = (await usecase.status()).acquisitions[0]
    assert running.running is True

    runtime.report_collect_failure("task-1:d1", "d1", "g", "read timeout")
    failed = (await usecase.status()).acquisitions[0]
    assert failed.running is False
    assert failed.consecutive_failures == 1
    assert failed.last_error == "read timeout"

    runtime.report_collect_started("task-1:d1", "d1", "g")
    runtime.report_collect_success("task-1:d1", "d1", "g", partial=False)
    recovered = (await usecase.status()).acquisitions[0]
    assert recovered.consecutive_failures == 0
    assert recovered.last_error is None

    await runtime.stop()


async def test_device_registry_changes_are_visible_immediately() -> None:
    runtime = _runtime(devices={"d1": _device("d1")}, protocols={"d1": _protocol()})
    usecase = QueryUseCase(runtime)

    assert {item.device_id for item in await usecase.list_devices()} == {"d1"}

    await runtime.add_device("d2", _device("d2"), _protocol(), [_point("p2")])
    assert {item.device_id for item in await usecase.list_devices()} == {"d1", "d2"}

    await runtime.remove_device("d1")
    assert {item.device_id for item in await usecase.list_devices()} == {"d2"}
