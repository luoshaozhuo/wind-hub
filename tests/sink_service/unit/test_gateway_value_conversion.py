"""Explicit Gateway target conversion must never silently mask a turbine fault."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from contracts import industrial_gateway_pb2 as gateway
from contracts.scalar_codec import decode_scalar
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.infrastructure.adapters.outbound.gateway import GatewayOutput

pytestmark = pytest.mark.unit


def _output() -> GatewayOutput:
    return GatewayOutput(
        endpoint="127.0.0.1:52075",
        gateway_id="target-01",
        owner_id="test",
        mappings={
            ("WTG01", "fault_status"): ("device_fault_active",),
            ("WTG01", "active_power"): ("sim_active_power",),
        },
        conversions={"device_fault_active": "int01_to_bool"},
        timeout_ms=1000,
        lease_ms=3000,
        max_batch_size=10,
    )


def _sample(point: str, value: int | float | bool) -> DeliveryPoint:
    return cast(
        DeliveryPoint,
        SimpleNamespace(device_id="WTG01", point_id=point, value=value),
    )


@pytest.mark.parametrize("value,expected", [(0, False), (1, True)])
def test_only_explicit_integer_flags_are_converted(value: int, expected: bool) -> None:
    output = _output()
    converted = output._target_value("device_fault_active", value)
    assert type(converted) is bool
    assert converted is expected
    assert output._target_value("sim_active_power", 1) == 1
    assert type(output._target_value("sim_active_power", 1)) is int


@pytest.mark.parametrize("value", [-1, 2, 0.0, 1.0, True, False, "0", "1", None])
def test_invalid_integer_fault_flags_are_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="integer 0 or 1"):
        _output()._target_value("device_fault_active", value)  # type: ignore[arg-type]


async def test_gateway_update_encodes_only_converted_boolean() -> None:
    class Client:
        async def UpdateVariables(self, request, *, timeout, metadata):  # noqa: N802
            values = {item.variable_id: decode_scalar(item.value) for item in request.values}
            assert values == {"device_fault_active": True, "sim_active_power": 450000.0}
            return gateway.UpdateVariablesResponse(
                results=[
                    gateway.VariableUpdateResult(variable_id=item.variable_id, success=True)
                    for item in request.values
                ]
            )

    output = _output()
    output._client = cast(object, Client())  # type: ignore[assignment]
    output._lease_token = "test-lease"
    result = await output.write_batch(
        (_sample("fault_status", 1), _sample("active_power", 450000.0))
    )
    assert result.delivered == (0, 1)
    assert result.error == ""


async def test_invalid_fault_value_never_reaches_gateway() -> None:
    output = _output()
    output._lease_token = "test-lease"
    output._client = cast(object, object())  # type: ignore[assignment]
    result = await output.write_batch((_sample("fault_status", 2),))
    assert result.delivered == ()
    assert result.permanent == (0,)
    assert result.error == "GATEWAY_CONVERSION_INVALID"


async def test_partial_batch_never_marks_invalid_sample_delivered() -> None:
    """部分转换失败：坏点不进 delivered，不写 False，好点正常交付。"""
    seen: list[dict[str, object]] = []

    class Client:
        async def UpdateVariables(self, request, *, timeout, metadata):  # noqa: N802
            seen.append({item.variable_id: decode_scalar(item.value) for item in request.values})
            return gateway.UpdateVariablesResponse(
                results=[
                    gateway.VariableUpdateResult(variable_id=item.variable_id, success=True)
                    for item in request.values
                ]
            )

    output = _output()
    output._client = cast(object, Client())  # type: ignore[assignment]
    output._lease_token = "test-lease"
    result = await output.write_batch(
        (_sample("fault_status", 2), _sample("active_power", 450000.0))
    )
    # 坏点（index0）绝不能被当作交付成功，也不能被改写成 False。
    assert result.delivered == (1,)
    assert 0 in result.settled
    assert result.permanent == (0,)
    assert result.error == "GATEWAY_CONVERSION_INVALID"
    assert len(seen) == 1
    assert "device_fault_active" not in seen[0]
    assert seen[0] == {"sim_active_power": 450000.0}


def _gateway_snapshot_docs(conversion: str | None) -> dict:
    from tests.sink_service._stage1_fixtures import snapshot_docs

    docs = snapshot_docs(
        include_templates=(
            "redis_realtime",
            "kafka_history",
            "realtime_power",
            "reliable_history",
            "gateway_realtime",
        )
    )
    sinks = dict(docs["configs/instances/sinks_prod-a_v1.0.0.yaml"]["sinks"])
    sinks["ems_gateway"] = {
        "template_id": "gateway_realtime",
        "template_version": "1.0.0",
        "connection": {"endpoint": "127.0.0.1:52075", "gateway_id": "gw-target"},
        "overrides": {},
    }
    docs["configs/instances/sinks_prod-a_v1.0.0.yaml"]["sinks"] = sinks
    mapping = {
        "source_device_id": "WTG01",
        "source_point_id": "fault_status",
        "variable_id": "device_fault_active",
    }
    if conversion is not None:
        mapping["conversion"] = conversion
    deliveries = dict(docs["configs/instances/deliveries_prod-a_v1.0.0.yaml"]["deliveries"])
    deliveries["ems_power_gw"] = {
        "template_id": "realtime_power",
        "template_version": "1.0.0",
        "sink_id": "ems_gateway",
        "desired_state": "START",
        "source": {
            "wind_farm_id": "farm-a",
            "collector_id": "collector-1",
            "devices": ["WTG01"],
            "points": ["fault_status"],
        },
        "target_mappings": [mapping],
        "overrides": {},
    }
    docs["configs/instances/deliveries_prod-a_v1.0.0.yaml"]["deliveries"] = deliveries
    return docs


def test_config_accepts_optional_explicit_conversion() -> None:
    from sink_service.infrastructure.config_io import validate_snapshot
    from tests.sink_service._stage1_fixtures import snapshot_files as _files

    docs = _gateway_snapshot_docs("int01_to_bool")
    snapshot = validate_snapshot(_files(docs))
    delivery = snapshot.deliveries["ems_power_gw"]
    assert delivery["target_mappings"][0]["conversion"] == "int01_to_bool"


def test_config_rejects_unsupported_conversion() -> None:
    from sink_service.domain.config import ConfigurationError
    from sink_service.infrastructure.config_io import validate_snapshot
    from tests.sink_service._stage1_fixtures import snapshot_files as _files

    docs = _gateway_snapshot_docs("bool_to_int01")
    with pytest.raises(ConfigurationError, match="unsupported conversion"):
        validate_snapshot(_files(docs))


def test_config_rejects_duplicate_target_variable() -> None:
    from sink_service.domain.config import ConfigurationError
    from sink_service.infrastructure.config_io import validate_snapshot
    from tests.sink_service._stage1_fixtures import snapshot_files

    docs = _gateway_snapshot_docs("int01_to_bool")
    second = dict(
        docs["configs/instances/deliveries_prod-a_v1.0.0.yaml"]["deliveries"]["ems_power_gw"][
            "target_mappings"
        ][0]
    )
    second["source_point_id"] = "other_point"
    docs["configs/instances/deliveries_prod-a_v1.0.0.yaml"]["deliveries"]["ems_power_gw"]["source"][
        "points"
    ] = ["fault_status", "other_point"]
    docs["configs/instances/deliveries_prod-a_v1.0.0.yaml"]["deliveries"]["ems_power_gw"][
        "target_mappings"
    ].append(second)
    with pytest.raises(ConfigurationError, match="duplicate Gateway variable"):
        validate_snapshot(snapshot_files(docs))


def test_sink_output_pool_signature_covers_conversions() -> None:
    """转换配置必须进入输出池签名：仅改 conversion 就必须重建适配器。"""
    from sink_service.infrastructure.config_io import validate_snapshot
    from sink_service.infrastructure.realtime_runtime import SinkOutputPool
    from tests.sink_service._stage1_fixtures import snapshot_files

    with_conv = validate_snapshot(snapshot_files(_gateway_snapshot_docs("int01_to_bool")))
    without = validate_snapshot(snapshot_files(_gateway_snapshot_docs(None)))
    adapter_a, signature_a = SinkOutputPool._build("ems_gateway", with_conv)
    adapter_b, signature_b = SinkOutputPool._build("ems_gateway", without)
    assert adapter_a._conversions == {"device_fault_active": "int01_to_bool"}
    assert adapter_b._conversions == {}
    assert signature_a != signature_b
