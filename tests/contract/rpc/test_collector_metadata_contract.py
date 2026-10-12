"""Collector 测点元数据查询：纯配置、版本一致性及不存在测点。"""

from __future__ import annotations

from core.infrastructure.rpc import collector_pb2 as pb


def test_metadata_protobuf_contract() -> None:
    service = pb.DESCRIPTOR.services_by_name["CollectorRuntimeService"]
    method = service.methods_by_name["GetPointsMetadata"]
    assert method.input_type.name == "PointsMetadataRequest"
    assert method.output_type.name == "PointsMetadataResponse"
    response = pb.PointsMetadataResponse(
        device_id="dev1",
        active_revision="startup",
        active_config_hash="hash",
        points=[pb.PointMetadataResult(point_id="missing", found=False)],
    )
    assert pb.PointsMetadataResponse.FromString(response.SerializeToString()) == response
