"""GrpcSink 通过强类型 Gateway WriteMany 发送测点，跳过坏质量。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.application.sink_contract import ResolvedSink
from core.domain.point_value import PointValue
from core.domain.sample_value import Quality
from core.infrastructure.rpc.scalar_codec import decode_scalar
from core.infrastructure.sink.grpc_sink import GrpcSink

pytestmark = pytest.mark.unit


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
    await sink.write(
        [
            PointValue("collector_wt", "power", 150.0, timestamp=timestamp),
            PointValue("collector_wt", "power", 999.0, Quality.BAD, timestamp),
        ]
    )
    assert outgoing.WriteMany.await_count == 1
    assert outgoing.WriteMany.await_args is not None
    request = outgoing.WriteMany.await_args.args[0]
    values = {
        (point.device_id, point.point_id): decode_scalar(point.value) for point in request.points
    }
    assert values == {("wt1", "active"): 150.0}
