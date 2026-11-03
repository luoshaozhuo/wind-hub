"""UC-09 Sink Delivery 查询与控制 System Test。"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from contracts import common_pb2
from contracts import sink_service_pb2 as sk

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _p(server: Any, method: str) -> str:
    return server.method_path("SinkService", method)


def test_sink_list_detail_roundtrip(cli_env: Any) -> None:
    """S01：deliveries 列表（channel 过滤上送）与 delivery 详情。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "ListDeliveries"),
        sk.ListDeliveriesResponse(
            deliveries=[sk.Delivery(delivery_id="dl1", channel="realtime", actual_state="RUNNING")]
        ),
    )
    server.respond_value(
        _p(server, "GetDelivery"),
        sk.GetDeliveryResponse(
            delivery=sk.Delivery(delivery_id="dl1", actual_state="RUNNING", revision="r1")
        ),
    )
    r1 = cli_env.subprocess_invoke("sink", "deliveries", "--channel", "realtime")
    assert r1.exit_code == 0, r1.stdout
    list_call = [c for c in server.calls if c.path.endswith("/ListDeliveries")][0]
    assert list_call.request.channel == "realtime"
    assert r1.json()["data"]["deliveries"][0]["delivery_id"] == "dl1"

    r2 = cli_env.subprocess_invoke("sink", "delivery", "dl1")
    assert r2.exit_code == 0, r2.stdout
    assert r2.json()["data"]["delivery"]["revision"] == "r1"


def test_sink_start_stop_revision_guard(cli_env: Any) -> None:
    """S02：start/stop 自动取 revision 并透传乐观锁；无 --yes 拒绝。"""
    server = cli_env.server
    denied = cli_env.subprocess_invoke("sink", "start", "dl1")
    assert denied.exit_code == 2
    assert not server.calls

    server.respond_value(
        _p(server, "GetDelivery"),
        sk.GetDeliveryResponse(delivery=sk.Delivery(delivery_id="dl1", revision="r2")),
    )
    server.respond_value(
        _p(server, "StartDelivery"),
        common_pb2.OperationResult(accepted=True, actual_state="STARTED"),
    )
    started = cli_env.subprocess_invoke("sink", "start", "dl1", "--yes")
    assert started.exit_code == 0, started.stdout
    start_call = [c for c in server.calls if c.path.endswith("/StartDelivery")][0]
    assert start_call.request.expected_revision == "r2"


def test_sink_stop_revision_conflict_exit1(cli_env: Any) -> None:
    """S03：停止时乐观锁冲突 → 1 且只发一次。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "GetDelivery"),
        sk.GetDeliveryResponse(delivery=sk.Delivery(delivery_id="dl1", revision="r3")),
    )
    server.fail(_p(server, "StopDelivery"), grpc.StatusCode.FAILED_PRECONDITION, "stale revision")
    result = cli_env.subprocess_invoke("sink", "stop", "dl1", "--yes")
    assert result.exit_code == 1, result.stdout
    assert len([c for c in server.calls if c.path.endswith("/StopDelivery")]) == 1
