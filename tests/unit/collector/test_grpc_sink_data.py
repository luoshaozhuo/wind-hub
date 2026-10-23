"""GrpcSink 通过标准 Struct 协议发送测点，跳过坏质量。"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.application.sink_contract import ResolvedSink
from core.domain.point_value import PointValue
from core.domain.sample_value import Quality
from core.infrastructure.rpc.gateway_data import decode_feedback
from core.infrastructure.sink.grpc_sink import GrpcSink


@pytest.mark.asyncio
async def test_grpc_sink_sends_mapped_good_samples() -> None:
    config = ResolvedSink(
        sink_id="gateway",
        kind="grpc",
        enabled=True,
        connection={"endpoint": "127.0.0.1:50073"},
        points=(
            {
                "source": {"device_id": "collector_wt", "point_id": "power"},
                "ref": "collector_wt.power",
                "source_data_type": "float32",
                "source_unit": "kW",
                "datatype": "float32",
                "unit": "kW",
                "address": {"device_id": "wt1", "point_id": "active"},
            },
        ),
    )
    sink = GrpcSink(config)
    outgoing = AsyncMock()
    sink._write_many = outgoing
    timestamp = datetime.now(UTC)
    await sink.write([
        PointValue("collector_wt", "power", 150.0, timestamp=timestamp),
        PointValue("collector_wt", "power", 999.0, Quality.BAD, timestamp),
    ])
    assert outgoing.await_count == 1
    assert outgoing.await_args is not None
    request = outgoing.await_args.args[0]
    assert decode_feedback(request) == {("wt1", "active"): 150.0}
