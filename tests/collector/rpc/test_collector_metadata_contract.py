"""Collector v2 测点元数据查询：纯配置、版本一致性及 wire 字段编号。"""

from __future__ import annotations

import pytest

from contracts import collector_v2_pb2 as pb

pytestmark = pytest.mark.contract


def test_metadata_protobuf_contract() -> None:
    service = pb.DESCRIPTOR.services_by_name["CollectorRuntimeService"]
    method = service.methods_by_name["GetPointMetadata"]
    assert method.input_type.name == "GetPointMetadataRequest"
    assert method.output_type.name == "GetPointMetadataResponse"
    response = pb.GetPointMetadataResponse(
        active_revision="startup",
        points=[pb.PointMetadata(point_id="rotor_speed", data_type="float32")],
    )
    assert pb.GetPointMetadataResponse.FromString(response.SerializeToString()) == response


def test_metadata_field_numbers_are_stable() -> None:
    """锁定 wire 消息字段编号，防止无意破坏兼容。"""
    request = pb.GetPointMetadataRequest.DESCRIPTOR
    assert request.fields_by_name["device_id"].number == 1
    assert request.fields_by_name["point_ids"].number == 2

    metadata = pb.PointMetadata.DESCRIPTOR
    expected = {
        "point_id": 1,
        "business_point_id": 2,
        "data_type": 3,
        "unit_id": 4,
        "quantity_id": 5,
        "access": 6,
        "description": 7,
        "scale": 8,
        "offset": 9,
        "protocol_address": 10,
    }
    assert {name: metadata.fields_by_name[name].number for name in expected} == expected

    response = pb.GetPointMetadataResponse.DESCRIPTOR
    assert response.fields_by_name["points"].number == 1
    assert response.fields_by_name["active_revision"].number == 2


def test_empty_points_round_trip() -> None:
    # v2 不返回 found=False 行：不存在测点即缺行（由调用方按 point_ids 对账）。
    row = pb.GetPointMetadataResponse(active_revision="rev-1")
    clone = pb.GetPointMetadataResponse.FromString(row.SerializeToString())
    assert clone == row
    assert list(clone.points) == []
