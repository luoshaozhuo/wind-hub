"""验证非风机设备快照能按 Gateway 映射发布，不依赖 Modbus 地址。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.infrastructure.rpc.scalar_codec import decode_scalar
from seahorse.domain.models import DeviceSnapshot, FarmSnapshot, SimulationTime
from seahorse.infrastructure.grpc_publisher import GrpcSnapshotPublisher

pytestmark = pytest.mark.unit


def test_external_snapshots_are_published_by_device_identity() -> None:
    publisher = GrpcSnapshotPublisher(
        "127.0.0.1:50073",
        {
            "substation": frozenset({"emsSyzAct"}),
            "dispatch": frozenset({"emsGridDispatchAct"}),
            "primary": frozenset({"emsFreDst"}),
            "inertia": frozenset({"actInertiaMode"}),
        },
    )
    send = Mock()
    publisher._stub = SimpleNamespace(WriteMany=send)
    try:
        publisher.publish(
            FarmSnapshot(
                SimulationTime(1, 0.5, 0.5),
                (
                    DeviceSnapshot("substation", {"emsSyzAct": 1200.0}),
                    DeviceSnapshot("dispatch", {"emsGridDispatchAct": 900.0}),
                    DeviceSnapshot("primary", {"emsFreDst": 300.0}),
                    DeviceSnapshot("inertia", {"actInertiaMode": False}),
                ),
            )
        )
        assert send.call_count == 1
        request = send.call_args.args[0]
        values = {
            (point.device_id, point.point_id): decode_scalar(point.value)
            for point in request.points
        }
        assert values == {
            ("substation", "emsSyzAct"): 1200.0,
            ("dispatch", "emsGridDispatchAct"): 900.0,
            ("primary", "emsFreDst"): 300.0,
            ("inertia", "actInertiaMode"): False,
        }
    finally:
        publisher.close()


def test_missing_external_snapshot_fails_before_grpc_write() -> None:
    publisher = GrpcSnapshotPublisher("127.0.0.1:50073", {"substation": frozenset({"emsSyzAct"})})
    send = Mock()
    publisher._stub = SimpleNamespace(WriteMany=send)
    try:
        with pytest.raises(KeyError, match="missing Gateway feedback"):
            publisher.publish(
                FarmSnapshot(
                    SimulationTime(1, 0.5, 0.5),
                    (DeviceSnapshot("substation", {"poi_voltage": 35000.0}),),
                )
            )
        send.assert_not_called()
    finally:
        publisher.close()
