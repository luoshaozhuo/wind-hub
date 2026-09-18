"""Unit tests for the QueryService application service.

验证对象：``application/query_service.py`` 的只读查询语义——
``read_point`` 区分「未知设备/点 → CommandError」与「读失败 → ProtocolError」，
``list_devices`` / ``get_device_info`` 依据协议健康状态返回 ``DeviceInfo``。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.query_service import QueryService
from wind_hub.config.schema import DeviceConfig, PointAddress, PointConfig
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort


def _device(device_id: str = "d1", protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(device_id=device_id, protocol=protocol, endpoint=Endpoint(host="h", port=1))


def _point(device_id: str = "d1", point_id: str = "p1") -> PointConfig:
    return PointConfig(point_id=point_id, device_id=device_id, address=PointAddress(type="hr"))


def _protocol(healthy: bool = True, read_values: list[PointValue] | None = None) -> MagicMock:
    proto = MagicMock(spec=ProtocolPort)
    proto.health = MagicMock(return_value=HealthStatus(healthy=healthy))
    proto.read = AsyncMock(return_value=read_values if read_values is not None else [])
    return proto


def _service(
    devices: dict[str, DeviceConfig] | None = None,
    protocols: dict[str, MagicMock] | None = None,
    points: dict[str, list[PointConfig]] | None = None,
) -> QueryService:
    return QueryService(
        scheduler=MagicMock(spec=Scheduler),
        protocols=protocols or {},
        devices=devices or {},
        points=points or {},
    )


def _value(device_id: str = "d1", point_id: str = "p1", value: float = 42.0) -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=value)


# ---------------------------------------------------------------------------
# read_point
# ---------------------------------------------------------------------------


async def test_read_point_returns_protocol_value() -> None:
    proto = _protocol(read_values=[_value(value=42.0)])
    service = _service(
        devices={"d1": _device()},
        protocols={"d1": proto},
        points={"d1": [_point()]},
    )

    value = await service.read_point("d1", "p1")

    assert value.value == 42.0
    proto.read.assert_awaited_once_with([PointRef(device_id="d1", point_id="p1")])


async def test_read_point_unknown_device_raises_command_error() -> None:
    service = _service(devices={"d1": _device()})

    with pytest.raises(CommandError, match="unknown device 'missing'"):
        await service.read_point("missing", "p1")


async def test_read_point_unknown_point_raises_command_error() -> None:
    service = _service(
        devices={"d1": _device()},
        protocols={"d1": _protocol()},
        points={"d1": [_point(point_id="p1")]},
    )

    with pytest.raises(CommandError, match="unknown point 'd1/nope'"):
        await service.read_point("d1", "nope")


async def test_read_point_missing_driver_raises_command_error() -> None:
    service = _service(
        devices={"d1": _device()},
        points={"d1": [_point()]},
    )

    with pytest.raises(CommandError, match="no protocol driver"):
        await service.read_point("d1", "p1")


async def test_read_point_empty_result_raises_protocol_error() -> None:
    proto = _protocol(read_values=[])
    service = _service(
        devices={"d1": _device()},
        protocols={"d1": proto},
        points={"d1": [_point()]},
    )

    with pytest.raises(ProtocolError):
        await service.read_point("d1", "p1")


# ---------------------------------------------------------------------------
# list_devices / get_device_info
# ---------------------------------------------------------------------------


async def test_list_devices_reports_health() -> None:
    service = _service(
        devices={"d1": _device("d1", "modbus"), "d2": _device("d2", "iec104")},
        protocols={"d1": _protocol(healthy=True), "d2": _protocol(healthy=False)},
    )

    infos = await service.list_devices()

    by_id = {i.device_id: i for i in infos}
    assert set(by_id) == {"d1", "d2"}
    assert by_id["d1"].connected is True
    assert by_id["d1"].protocol == "modbus"
    assert by_id["d2"].connected is False


async def test_list_devices_missing_driver_reports_disconnected() -> None:
    service = _service(devices={"d1": _device()})

    infos = await service.list_devices()

    assert len(infos) == 1
    assert infos[0].connected is False
    assert infos[0].last_seen is None


async def test_get_device_info_known() -> None:
    service = _service(
        devices={"d1": _device()},
        protocols={"d1": _protocol(healthy=True)},
    )

    info = await service.get_device_info("d1")

    assert info.device_id == "d1"
    assert info.connected is True


async def test_get_device_info_unknown_raises_command_error() -> None:
    service = _service(devices={"d1": _device()})

    with pytest.raises(CommandError, match="unknown device 'missing'"):
        await service.get_device_info("missing")
