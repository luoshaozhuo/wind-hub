"""网关遥测发送只允许显式反馈映射，拒绝半有效快照。"""
from __future__ import annotations

import pytest

from seahorse.domain.models import DeviceSnapshot, FarmSnapshot, SimulationTime
from seahorse.infrastructure.modbus_publisher import (
    GatewayModbusPublisher,
    PublishConfig,
    PublishPoint,
    _encode,
)


class _Response:
    def isError(self) -> bool:
        return False


class _Client:
    def __init__(self) -> None:
        self.writes: list[tuple[int, list[int], int]] = []

    def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> _Response:
        self.writes.append((address, values, device_id))
        return _Response()

    def write_coil(self, address: int, value: bool, *, device_id: int) -> _Response:
        self.writes.append((address, [int(value)], device_id))
        return _Response()


def test_cdab_word_order() -> None:
    point = PublishPoint(
        device_id="d10001", point_id="emsPowAct", host="127.0.0.1",
        port=15020, unit_id=1, address=41557, word_order="little_endian"
    )
    assert _encode(point, 1000.0) == (0, 0x447A)


def test_feedback_write_only_explicit_point() -> None:
    client = _Client()
    point = PublishPoint(
        device_id="d10001", point_id="emsPowAct", host="127.0.0.1",
        port=15020, unit_id=1, address=41500
    )
    publisher = GatewayModbusPublisher(
        PublishConfig(points=[point]), client_factory=lambda _h, _p: client
    )
    snapshot = FarmSnapshot(
        SimulationTime(1, 0.5, 0.5),
        (DeviceSnapshot("d10001", {"emsPowAct": 1000.0}),),
    )
    publisher.publish(snapshot)
    assert client.writes == [(41500, [0x447A, 0], 1)]
    with pytest.raises(KeyError, match="missing feedback"):
        publisher.publish(
            FarmSnapshot(SimulationTime(2, 1.0, 0.5), (DeviceSnapshot("d10001", {}),))
        )
