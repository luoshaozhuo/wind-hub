"""Sink GatewayOutput 的真实 Bearer 租约与变量写入链。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import grpc
import pytest

from contracts import industrial_gateway_pb2 as gateway_pb
from contracts.scalar_codec import decode_scalar
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.infrastructure.adapters.outbound.gateway import GatewayOutput
from tests.support.bearer import ensure_bearer
from tests.support.gateway import (
    FEEDBACK_FLOAT,
    start_gateway,
    two_server_specs,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


def _output(endpoint: str, principal: str) -> GatewayOutput:
    return GatewayOutput(
        endpoint=endpoint,
        credential_ref=ensure_bearer(principal),
        gateway_id="gw1",
        owner_id="sink-system-test",
        mappings={("D01", "source_power"): (FEEDBACK_FLOAT,)},
        timeout_ms=2000,
        lease_ms=2000,
        max_batch_size=16,
    )


def _sample() -> DeliveryPoint:
    now = datetime.now(UTC)
    return DeliveryPoint(
        data_level="business",
        batch_id="batch-test",
        device_type="turbine",
        point_table_id="point_table_modbus_wtg_v1.0.0",
        business_point_id="active_power",
        unit="kilowatt",
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-1",
        device_id="D01",
        point_id="source_power",
        sample_id="sample-1",
        value=12.5,
        quality="good",
        sample_time=now,
        publish_time=now,
        receive_time=now,
        max_staleness_ms=60_000,
    )


async def test_sink_gateway_bearer_lease_update_and_release(tmp_path: Path) -> None:
    harness = await start_gateway(
        tmp_path / "root",
        two_server_specs(control_enabled=False),
        mtls={
            "gw-admin": [
                "read",
                "inject",
                "lifecycle",
                "diagnose",
                "config_read",
                "config_write",
            ],
            "sink-writer": ["read", "inject"],
            "sink-reader": ["read"],
        },
        bootstrap_cn="gw-admin",
        extra_clients=["sink-stranger"],
    )
    writer = _output(f"127.0.0.1:{harness.grpc_port}", "sink-writer")
    try:
        await writer.open()
        # lease_ms / 3 后至少发生一次真实 RENEW，再验证写入仍可用。
        await asyncio.sleep(0.8)
        outcome = await writer.write_batch((_sample(),))
        assert outcome.error == ""
        assert outcome.delivered == (0,)
        reply = await harness.gateway_stub().ReadVariables(
            gateway_pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_FLOAT])
        )
        assert decode_scalar(reply.values[0].value) == 12.5
    finally:
        await writer.close()

    reader = _output(f"127.0.0.1:{harness.grpc_port}", "sink-reader")
    with pytest.raises(grpc.aio.AioRpcError) as permission_denied:
        await reader.open()
    assert permission_denied.value.code() == grpc.StatusCode.PERMISSION_DENIED

    stranger = _output(f"127.0.0.1:{harness.grpc_port}", "sink-stranger")
    with pytest.raises(grpc.aio.AioRpcError) as unauthenticated:
        await stranger.open()
    assert unauthenticated.value.code() == grpc.StatusCode.UNAUTHENTICATED
    await harness.stop()
