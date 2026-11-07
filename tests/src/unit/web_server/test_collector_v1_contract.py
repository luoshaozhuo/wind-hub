"""Web Server Collector 出站边界必须使用现行 Collector/Common 契约。"""

from __future__ import annotations

import asyncio

import pytest

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc, common_pb2_grpc
from web_server.adapter.outbound.grpc.collector import (
    CollectorGrpcClient,
    _task_instance,
    _task_summary,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def test_wire_stubs_match_authoritative_services() -> None:
    assert hasattr(collector_pb2_grpc, "CollectorServiceStub")
    assert hasattr(common_pb2_grpc, "CommonServiceStub")

    async def check() -> None:
        client = CollectorGrpcClient("127.0.0.1:1")
        try:
            assert isinstance(client._collector, collector_pb2_grpc.CollectorServiceStub)
            assert isinstance(client._common, common_pb2_grpc.CommonServiceStub)
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("RUNNING", "running"),
        ("STOPPED", "stopped"),
        ("FAULTED", "failed"),
        ("BLOCKED", "failed"),
    ],
)
def test_task_state_from_v1_proto(state: str, expected: str) -> None:
    message = pb.Task(
        task_id="turbine-1",
        device_id="turbine-1",
        desired_state="START",
        actual_state=state,
    )
    assert _task_summary(message).runtime_state == expected
    assert _task_instance(message).state == expected
