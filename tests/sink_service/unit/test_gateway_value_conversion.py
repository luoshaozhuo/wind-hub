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


@pytest.mark.parametrize("value", [-1, 2, 0.0, 1.0, True, False, "1"])
def test_invalid_integer_fault_flags_are_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="integer 0 or 1"):
        _output()._target_value("device_fault_active", value)  # type: ignore[arg-type]


async def test_gateway_update_encodes_only_converted_boolean() -> None:
    class Client:
        async def UpdateVariables(self, request, *, timeout, metadata):
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
