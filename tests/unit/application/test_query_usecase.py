"""QueryUseCase 的单元测试。

验证对象：``application/usecase/query.py``——只读查询全部穿透到 Runtime
当前注册表（不缓存静态快照）。

覆盖点：

- ``read_point``：区分「未知设备/点 → CommandError」与「读失败 →
  ProtocolError」；
- ``list_devices`` / ``get_device_info``：按当前注册表与协议健康状态；
- ``status()``：系统快照聚合（running、组件计数、健康切分、点位统计）；
- ``status().acquisitions``：按 Task Instance 粒度的业务执行状态
  （instance_id/task_id/device_id/point_group/running/
  consecutive_failures/last_error/last_duration）——经 Runtime 的
  AcquisitionStatePort 公开上报方法真实驱动状态演进；
- **热重载可见性**：``Runtime.add_device`` 后查询立即可见新设备；
  ``rebuild_device`` 替换协议驱动后 ``read_point`` 使用新驱动；
  点表变更后 ``read_point`` 按最新点表校验；``remove_device`` 后不可读。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub.application.port.sink import SinkPort
from wind_hub.application.runtime import Runtime
from wind_hub.application.runtime.device import Device
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub.config.schema import (
    CollectionTaskConfig,
    DeviceConfig,
    PointAddress,
    PointConfig,
    RuntimeConfig,
    TaskTarget,
)
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort

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


def _protocol(healthy: bool = True, read_values: list[PointValue] | None = None) -> MagicMock:
    proto = MagicMock(spec=ProtocolPort)
    proto.health = MagicMock(return_value=HealthStatus(healthy=healthy))
    proto.read = AsyncMock(return_value=read_values if read_values is not None else [])
    proto.connect = AsyncMock()
    proto.close = AsyncMock()
    return proto


def _build_device_map(
    devices: dict[str, DeviceConfig],
    protocols: dict[str, MagicMock],
    points: dict[str, list[PointConfig]],
) -> dict[str, Device]:
    """按装配语义聚合 Device：配置 + 点表 + 协议实例。"""
    return {
        device_id: Device(cfg, points.get(device_id, []), protocols[device_id])
        for device_id, cfg in devices.items()
    }


def _sink(healthy: bool = True) -> MagicMock:
    sink = MagicMock(spec=SinkPort)
    sink.health = MagicMock(return_value=HealthStatus(healthy=healthy))
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.flush = AsyncMock()
    return sink


def _value(device_id: str = "d1", point_id: str = "p1", value: float = 42.0) -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=value)


def _runtime(
    devices: dict[str, DeviceConfig] | None = None,
    protocols: dict[str, MagicMock] | None = None,
    sinks: dict[str, MagicMock] | None = None,
    points: dict[str, list[PointConfig]] | None = None,
    tasks: dict[str, CollectionTaskConfig] | None = None,
) -> Runtime:
    devices = devices if devices is not None else {}
    protocols = protocols if protocols is not None else {}
    points = points if points is not None else {}
    # 没有协议实例的设备使用默认 mock——Runtime 只持有聚合后的 Device。
    full_protocols = {device_id: protocols.get(device_id) or _protocol() for device_id in devices}
    device_map = _build_device_map(devices, full_protocols, points)
    engine = AcquisitionEngine()
    return Runtime(
        devices=device_map,
        sinks=sinks if sinks is not None else {},
        engine=engine,
        dispatcher=MagicMock(spec=CommandDispatcher),
        config=RuntimeConfig(),
        tasks=tasks,
    )


# ---------------------------------------------------------------------------
# read_point
# ---------------------------------------------------------------------------


async def test_read_point_returns_protocol_value() -> None:
    proto = _protocol(read_values=[_value(value=42.0)])
    usecase = QueryUseCase(
        _runtime(devices={"d1": _device()}, protocols={"d1": proto}, points={"d1": [_point()]})
    )

    value = await usecase.read_point("d1", "p1")

    assert value.value == 42.0
    proto.read.assert_awaited_once_with([PointRef(device_id="d1", point_id="p1")])


async def test_read_point_unknown_device_raises_command_error() -> None:
    usecase = QueryUseCase(_runtime(devices={"d1": _device()}))

    with pytest.raises(CommandError, match="unknown device 'missing'"):
        await usecase.read_point("missing", "p1")


async def test_read_point_unknown_point_raises_command_error() -> None:
    usecase = QueryUseCase(
        _runtime(
            devices={"d1": _device()},
            protocols={"d1": _protocol()},
            points={"d1": [_point(point_id="p1")]},
        )
    )

    with pytest.raises(CommandError, match="unknown point 'd1/nope'"):
        await usecase.read_point("d1", "nope")


async def test_read_point_empty_result_raises_protocol_error() -> None:
    proto = _protocol(read_values=[])
    usecase = QueryUseCase(
        _runtime(devices={"d1": _device()}, protocols={"d1": proto}, points={"d1": [_point()]})
    )

    with pytest.raises(ProtocolError):
        await usecase.read_point("d1", "p1")


# ---------------------------------------------------------------------------
# list_devices / get_device_info
# ---------------------------------------------------------------------------


async def test_list_devices_reports_health() -> None:
    usecase = QueryUseCase(
        _runtime(
            devices={"d1": _device("d1", "modbus"), "d2": _device("d2", "iec104")},
            protocols={"d1": _protocol(healthy=True), "d2": _protocol(healthy=False)},
        )
    )

    infos = await usecase.list_devices()

    by_id = {i.device_id: i for i in infos}
    assert set(by_id) == {"d1", "d2"}
    assert by_id["d1"].connected is True
    assert by_id["d1"].protocol == "modbus"
    assert by_id["d2"].connected is False


async def test_get_device_info_known() -> None:
    usecase = QueryUseCase(
        _runtime(devices={"d1": _device()}, protocols={"d1": _protocol(healthy=True)})
    )

    info = await usecase.get_device_info("d1")

    assert info.device_id == "d1"
    assert info.connected is True


async def test_get_device_info_unknown_raises_command_error() -> None:
    usecase = QueryUseCase(_runtime(devices={"d1": _device()}))

    with pytest.raises(CommandError, match="unknown device 'missing'"):
        await usecase.get_device_info("missing")


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


async def test_status_aggregates_runtime_snapshot() -> None:
    usecase = QueryUseCase(
        _runtime(
            devices={"d1": _device("d1"), "d2": _device("d2")},
            protocols={"d1": _protocol(healthy=True), "d2": _protocol(healthy=False)},
            sinks={"s1": _sink(healthy=True), "s2": _sink(healthy=False)},
        )
    )

    status = await usecase.status()

    assert status.running is False  # runtime 未 start
    assert status.device_count == 2
    assert status.sink_count == 2
    assert status.devices_connected == 1
    assert status.sinks_healthy == 1
    assert status.points_collected == 0
    assert status.points_routed == 0
    assert status.points_dropped == 0
    assert status.acquisitions == []  # 未 start，无 Task Instance 注册


async def test_status_acquisitions_reflect_task_instance_states() -> None:
    """acquisitions 按 Task Instance 粒度映射 Runtime 的采集执行状态。

    状态演进全部经 Runtime 的公开 AcquisitionStatePort 方法
    （report_collect_started / report_collect_failure）真实驱动。
    """
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": _protocol()},
        points={"d1": [_point()]},
        tasks={"task-1": _task("task-1", device="d1")},
    )
    await runtime.start()  # 注册 Task Instance（默认 STOPPED，无 polling 协程）
    usecase = QueryUseCase(runtime)

    status = await usecase.status()
    assert status.running is True
    assert len(status.acquisitions) == 1
    acq = status.acquisitions[0]
    assert acq.instance_id == "task-1:d1"
    assert acq.task_id == "task-1"
    assert acq.device_id == "d1"
    assert acq.point_group == "g"
    assert acq.running is False
    assert acq.consecutive_failures == 0
    assert acq.last_error is None
    assert acq.last_duration is None

    # 一次 collect 在飞 → running=True
    runtime.report_collect_started("task-1:d1", "d1", "g")
    acq = (await usecase.status()).acquisitions[0]
    assert acq.running is True

    # 失败收尾 → running 归位、失败计数与错误如实呈现
    runtime.report_collect_failure("task-1:d1", "d1", "g", "read timeout")
    acq = (await usecase.status()).acquisitions[0]
    assert acq.running is False
    assert acq.consecutive_failures == 1
    assert acq.last_error == "read timeout"
    assert acq.last_duration is not None
    assert acq.last_duration >= 0.0

    # 成功收尾 → 连续失败清零、错误清空
    runtime.report_collect_started("task-1:d1", "d1", "g")
    runtime.report_collect_success("task-1:d1", "d1", "g", partial=False)
    acq = (await usecase.status()).acquisitions[0]
    assert acq.running is False
    assert acq.consecutive_failures == 0
    assert acq.last_error is None

    await runtime.stop()


# ---------------------------------------------------------------------------
# 热重载可见性
# ---------------------------------------------------------------------------


async def test_hot_added_device_visible_immediately() -> None:
    """Runtime.add_device 后 list_devices/read_point 立即看到新设备。"""
    runtime = _runtime(devices={"d1": _device("d1")}, protocols={"d1": _protocol()})
    usecase = QueryUseCase(runtime)

    assert {i.device_id for i in await usecase.list_devices()} == {"d1"}

    p_new = _protocol(read_values=[_value(device_id="d2", point_id="t1", value=7.0)])
    await runtime.add_device("d2", _device("d2"), p_new, [_point("t1")])

    assert {i.device_id for i in await usecase.list_devices()} == {"d1", "d2"}
    value = await usecase.read_point("d2", "t1")
    assert value.value == 7.0


async def test_rebuilt_device_read_uses_new_protocol() -> None:
    """rebuild_device 替换协议驱动后，realtime read 走新驱动而非旧驱动。"""
    p_old = _protocol(read_values=[_value(value=1.0)])
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": p_old},
        points={"d1": [_point("p1")]},
    )
    usecase = QueryUseCase(runtime)

    p_new = _protocol(read_values=[_value(value=2.0)])
    await runtime.rebuild_device("d1", _device("d1"), p_new, [_point("p1")])

    value = await usecase.read_point("d1", "p1")

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
    usecase = QueryUseCase(runtime)

    await runtime.rebuild_device("d1", _device("d1"), proto, [_point("p2")])

    with pytest.raises(CommandError, match="unknown point 'd1/p1'"):
        await usecase.read_point("d1", "p1")
    value = await usecase.read_point("d1", "p2")
    assert value.value == 3.0


async def test_removed_device_no_longer_listed_or_readable() -> None:
    proto = _protocol()
    runtime = _runtime(
        devices={"d1": _device("d1")},
        protocols={"d1": proto},
        points={"d1": [_point("p1")]},
    )
    usecase = QueryUseCase(runtime)

    await runtime.remove_device("d1")

    assert await usecase.list_devices() == []
    with pytest.raises(CommandError, match="unknown device 'd1'"):
        await usecase.read_point("d1", "p1")
