"""Collector 测点元数据查询：纯配置、版本一致性及不存在测点。"""

from __future__ import annotations

import pytest

from contracts import collector_pb2 as pb

pytestmark = pytest.mark.contract


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


def test_metadata_field_numbers_are_stable() -> None:
    """锁定新增 wire 消息的字段编号，防止无意破坏兼容。"""
    request = pb.PointsMetadataRequest.DESCRIPTOR
    assert request.fields_by_name["device_id"].number == 1
    assert request.fields_by_name["point_ids"].number == 2

    metadata = pb.PointMetadata.DESCRIPTOR
    expected = {
        "point_id": 1,
        "access": 2,
        "data_type": 3,
        "unit": 4,
        "scale": 5,
        "offset": 6,
        "configured_address": 7,
        "task_ids": 8,
        "sink_ids": 9,
    }
    assert {name: metadata.fields_by_name[name].number for name in expected} == expected

    result = pb.PointMetadataResult.DESCRIPTOR
    assert result.fields_by_name["point_id"].number == 1
    assert result.fields_by_name["found"].number == 2
    assert result.fields_by_name["metadata"].number == 3
    assert result.fields_by_name["error"].number == 4

    response = pb.PointsMetadataResponse.DESCRIPTOR
    assert response.fields_by_name["device_id"].number == 1
    assert response.fields_by_name["active_revision"].number == 2
    assert response.fields_by_name["active_config_hash"].number == 3
    assert response.fields_by_name["points"].number == 4


def test_found_false_round_trip_omits_metadata() -> None:
    row = pb.PointMetadataResult(point_id="p1", found=False, error="point not found")
    clone = pb.PointMetadataResult.FromString(row.SerializeToString())
    assert clone == row
    assert not clone.HasField("metadata")
