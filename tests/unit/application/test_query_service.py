"""QueryService 的单元测试。

验证对象：``application/query_service.py``——只读查询全部穿透到 Runtime
当前注册表（不缓存静态快照）。

覆盖点：

- ``read_point``：区分「未知设备/点 → CommandError」与「读失败 →
  ProtocolError」；
- ``list_devices`` / ``get_device_info``：按当前注册表与协议健康状态；
- ``status()``：系统快照聚合（running、组件计数、健康切分、点位统计）；
- **热重载可见性**：``Runtime.add_device`` 后查询立即可见新设备；
  ``rebuild_device`` 替换协议驱动后 ``read_point`` 使用新驱动；
  点表变更后 ``read_point`` 按最新点表校验。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.query_service import QueryService
from wind_hub.application.runtime import Runtime
from wind_hub.config.schema import (
    DeviceConfig,
    PointAddress,
    PointConfig,
    SchedulerConfig,
)
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.command import Dispatcher
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort
from wind_hub.domain.port.scheduling import SchedulerPort
from wind_hub.domain.processing import Pipeline
from wind_hub.domain.routing import Router

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device(device_id: str = "d1", protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        point_table="t1",
        endpoint=Endpoint(host="h", port=1),
    )


def _point(point_id: str = "p1") -> PointConfig:
    return PointConfig(point_id=point_id, address=PointAddress(type="hr"))


def _protocol(healthy: bool = True, read_values: list[PointValue] | None = None) -> MagicMock:
    proto = MagicMock(spec=ProtocolPort)
    proto.health = MagicMock(return_value=HealthStatus(healthy=healthy))
    proto.read = AsyncMock(return_value=read_values if read_values is not None else [])
    proto.connect = AsyncMock()
    proto.close = AsyncMock()
    return proto


def _value(device_id: str = "d1", point_id: str = "p1", value: float = 42.0) -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=value)


def _runtime(
    devices: dict[str, DeviceConfig] | None = None,
    protocols: dict[str, MagicMock] | None = None,
    points: dict[str, list[PointConfig]] | None = None,
) -> Runtime:
    protocols = protocols if protocols is not None else {}
    engine = AcquisitionEngine(
        protocols=protocols,
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        points_by_device=points if points is not None else {},
    )
    scheduler = MagicMock(spec=SchedulerPort)
    scheduler.list_jobs.return_value = []
    return Runtime(
        devices=devices if devices is not None else {},
        protocols=protocols,
        sinks={},
        engine=engine,
        scheduler=scheduler,
        dispatcher=MagicMock(spec=Dispatcher),
        config=SchedulerConfig(),
        points_by_device=points,
    )


# ---------------------------------------------------------------------------
# read_point
# ---------------------------------------------------------------------------


async def test_read_point_returns_protocol_value() -> None:
    proto = _protocol(read_values=[_value(value=42.0)])
    service = QueryService(
        _runtime(devices={"d1": _device()}, protocols={"d1": proto}, points={"d1": [_point()]})
    )

    value = await service.read_point("d1", "p1")

    assert value.value == 42.0
    proto.read.assert_awaited_once_with([PointRef(device_id="d1", point_id="p1")])


async def test_read_point_unknown_device_raises_command_error() -> None:
    service = QueryService(_runtime(devices={"d1": _device()}))

    with pytest.raises(CommandError, match="unknown device 'missing'"):
        await service.read_point("missing", "p1")


async def test_read_point_unknown_point_raises_command_error() -> None:
    service = QueryService(
        _runtime(
            devices={"d1": _device()},
            protocols={"d1": _protocol()},
            points={"d1": [_point(point_id="p1")]},
        )
    )

    with pytest.raises(CommandError, match="unknown point 'd1/nope'"):
        await service.read_point("d1", "nope")


async def test_read_point_missing_driver_raises_command_error() -> None:
    service = QueryService(_runtime(devices={"d1": _device()}, points={"d1": [_point()]}))

    with pytest.raises(CommandError, match="no protocol driver"):
        await service.read_point("d1", "p1")


async def test_read_point_empty_result_raises_protocol_error() -> None:
    proto = _protocol(read_values=[])
    service = QueryService(
        _runtime(devices={"d1": _device()}, protocols={"d1": proto}, points={"d1": [_point()]})
    )

    with pytest.raises(ProtocolError):
        await service.read_point("d1", "p1")


# ---------------------------------------------------------------------------
# list_devices / get_device_info
# ---------------------------------------------------------------------------


async def test_list_devices_reports_health() -> None:
    service = QueryService(
        _runtime(
            devices={"d1": _device("d1", "modbus"), "d2": _device("d2", "iec104")},
            protocols={"d1": _protocol(healthy=True), "d2": _protocol(healthy=False)},
        )
    )

    infos = await service.list_devices()

    by_id = {i.device_id: i for i in infos}
    assert set(by_id) == {"d1", "d2"}
    assert by_id["d1"].connected is True
    assert by_id["d1"].protocol == "modbus"
    assert by_id["d2"].connected is False


async def test_list_devices_missing_driver_reports_disconnected() -> None:
    service = QueryService(_runtime(devices={"d1": _device()}))

    infos = await service.list_devices()

    assert len(infos) == 1
    assert infos[0].connected is False
    assert infos[0].last_seen is None


async def test_get_device_info_known() -> None:
    service = QueryService(
        _runtime(devices={"d1": _device()}, protocols={"d1": _protocol(healthy=True)})
    )

    info = await service.get_device_info("d1")

    assert info.device_id == "d1"
    assert info.connected is True


async def test_get_device_info_unknown_raises_command_error() -> None:
    service = QueryService(_runtime(devices={"d1": _device()}))

    with pytest.raises(CommandError, match="unknown device 'missing'"):
        await service.get_device_info("missing")


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


async def test_status_aggregates_runtime_snapshot() -> None:
    service = QueryService(
        _runtime(
            devices={"d1": _device("d1"), "d2": _device("d2")},
            protocols={"d1": _protocol(healthy=True), "d2": _protocol(healthy=False)},
        )
    )

    status = await service.status()

    assert status.running is False  # runtime 未 start
    assert status.device_count == 2
    assert status.sink_count == 0
    assert status.devices_connected == 1
    assert status.sinks_healthy == 0
    assert status.points_collected == 0
    assert status.points_routed == 0
    assert status.points_dropped == 0


# ---------------------------------------------------------------------------
# 热重载可见性
# ---------------------------------------------------------------------------


async def test_hot_added_device_visible_immediately() -> None:
    """Runtime.add_device 后 list_devices/read_point 立即看到新设备。"""
    runtime = _runtime(devices={"d1": _device("d1")}, protocols={"d1": _protocol()})
    service = QueryService(runtime)

    assert {i.device_id for i in await service.list_devices()} == {"d1"}

    p_new = _protocol(read_values=[_value(device_id="d2", point_id="t1", value=7.0)])
    await runtime.add_device("d2", _device("d2"), p_new, [_point("t1")])

    assert {i.device_id for i in await service.list_devices()} == {"d1", "d2"}
    value = await service.read_point("d2", "t1")
    assert value.value == 7.0


async def test_rebuilt_device_read_uses_new_protocol() -> None:
    """rebuild_device 替换协议驱动后，realtime read 走新驱动而非旧驱动。"""
    p_old = _protocol(read_values=[_value(value=1.0)])
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": p_old},
        points={"d1": [_point("p1")]},
    )
    service = QueryService(runtime)

    p_new = _protocol(read_values=[_value(value=2.0)])
    await runtime.rebuild_device("d1", _device("d1"), p_new, [_point("p1")])

    value = await service.read_point("d1", "p1")

    assert value.value == 2.0
    p_new.read.assert_awaited_once()
    assert p_old.read.await_count == 0


async def test_point_table_change_visible_to_read_validation() -> None:
    """点表变更后 read_point 按最新点表校验：旧点失效、新点可读。"""
    proto = _protocol(read_values=[_value(point_id="p2", value=3.0)])
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": proto},
        points={"d1": [_point("p1")]},
    )
    service = QueryService(runtime)

    await runtime.rebuild_device("d1", _device("d1"), proto, [_point("p2")])

    with pytest.raises(CommandError, match="unknown point 'd1/p1'"):
        await service.read_point("d1", "p1")
    value = await service.read_point("d1", "p2")
    assert value.value == 3.0


async def test_removed_device_no_longer_listed_or_readable() -> None:
    proto = _protocol()
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": proto},
        points={"d1": [_point("p1")]},
    )
    service = QueryService(runtime)

    await runtime.remove_device("d1")

    assert await service.list_devices() == []
    with pytest.raises(CommandError, match="unknown device 'd1'"):
        await service.read_point("d1", "p1")
