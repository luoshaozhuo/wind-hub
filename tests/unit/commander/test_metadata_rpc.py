"""Commander metadata gRPC wire compatibility checks."""

from __future__ import annotations

from types import SimpleNamespace

from commander.application.metadata import CommanderMetadataService
from commander.application.runtime import CommanderRuntime
from commander.infrastructure.grpc.server import CommanderService
from core.infrastructure.rpc import commander_pb2 as pb
from tests.support.new_commander import FakeRegistry, make_commander_config


def test_metadata_wire_schema_and_roundtrip() -> None:
    service = pb.DESCRIPTOR.services_by_name["CommanderService"]
    method = service.methods_by_name["GetPointsMetadata"]
    assert method.input_type.full_name == "windhub.commander.v1.PointsMetadataRequest"
    assert method.output_type.full_name == "windhub.commander.v1.PointsMetadataResponse"
    assert pb.PointVerifyResponse.DESCRIPTOR.fields_by_name["access"].number == 20

    response = pb.PointsMetadataResponse(
        device_id="device-1",
        active_revision="r2",
        active_config_hash="hash-b",
        points=[
            pb.PointMetadataResult(
                point_id="input",
                found=True,
                metadata=pb.PointMetadata(
                    point_id="input",
                    access="read",
                    data_type="float32",
                    unit="m/s",
                    scale=1.0,
                    offset=0.0,
                ),
            ),
            pb.PointMetadataResult(point_id="missing", found=False, error="point not found"),
        ],
    )
    copy = pb.PointsMetadataResponse.FromString(response.SerializeToString())
    assert copy == response
    assert copy.points[0].metadata.access == "read"
    assert copy.points[1].found is False


def test_diagnostic_point_access_wire_roundtrip() -> None:
    point = pb.PointVerifyResponse(device_id="dev1", point_id="p1", access="read_write")
    copy = pb.PointVerifyResponse.FromString(point.SerializeToString())
    assert copy.access == "read_write"


async def test_grpc_facade_returns_pinned_config_metadata_without_device_io() -> None:
    registry = FakeRegistry()
    runtime = CommanderRuntime(
        make_commander_config(),
        config_hash="hash-a",
        protocol_registry=registry,
    )
    app = SimpleNamespace(metadata=CommanderMetadataService(runtime))
    service = CommanderService(app)  # type: ignore[arg-type]
    result = await service.GetPointsMetadata(
        pb.PointsMetadataRequest(device_id="dev1", point_ids=["p1", "missing"]),
        None,  # type: ignore[arg-type]
    )
    assert result.active_config_hash == "hash-a"
    assert result.active_revision == "startup"
    assert [item.found for item in result.points] == [True, False]
    assert result.points[0].metadata.access == "read_write"
    assert result.points[0].metadata.data_type == "float32"
    assert registry.instances[0].connect_calls == 0
    assert registry.instances[0].writes == []
