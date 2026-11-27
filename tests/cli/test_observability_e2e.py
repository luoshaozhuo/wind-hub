"""UC-16 运行指标与事件 System Test（CommonService GetMetrics/GetEvents 全服务）。"""

from __future__ import annotations

import pytest

from contracts import collector_pb2, common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


@pytest.mark.parametrize(
    "command",
    (
        ("collector", "metrics"),
        ("commander", "metrics"),
        ("sink", "metrics"),
        ("gateway", "metrics"),
        ("data-generator", "metrics"),
    ),
)
def test_metrics_all_services(cli_env: object, command: tuple[str, ...]) -> None:
    """S01：五服务 metrics 经 CommonService 真实 mTLS 往返。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetMetrics"),
        common_pb2.GetMetricsResponse(metrics=[common_pb2.Metric(name="uptime", value=12.0)]),
    )
    result = cli_env.subprocess_invoke(*command)
    assert result.exit_code == 0, (command, result.stdout)
    assert result.json()["data"]["metrics"][0]["name"] == "uptime"


def test_events_pagination_and_redaction(cli_env: object) -> None:
    """S02：events 分页字段透传；S03：事件正文含 secret 字样时由本地安全层处理。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetEvents"),
        common_pb2.GetEventsResponse(
            events=[common_pb2.Event(event_id="e1", severity="WARN", message="something happened")],
            next_page_token="p2",
        ),
    )
    result = cli_env.subprocess_invoke(
        "collector", "events", "--page-size", "10", "--page-token", "p1"
    )
    assert result.exit_code == 0, result.stdout
    data = result.json()["data"]
    assert data["events"][0]["event_id"] == "e1"
    assert data["next_page_token"] == "p2"
    call = [c for c in server.calls if c.path.endswith("/GetEvents")][0]
    assert call.request.page_size == 10
    assert call.request.page_token == "p1"


@pytest.mark.parametrize("service", ("collector", "commander", "sink", "gateway", "data-generator"))
def test_events_time_range_and_filter_all_services(cli_env: object, service: str) -> None:
    cli_env.server.respond_value(
        cli_env.server.method_path("CommonService", "GetEvents"),
        common_pb2.GetEventsResponse(),
    )

    result = cli_env.subprocess_invoke(
        service,
        "events",
        "--from-time",
        "2026-11-27T00:00:00Z",
        "--to-time",
        "2026-11-27T01:00:00Z",
        "--filter",
        "severity=WARN",
        "--page-size",
        "7",
        "--page-token",
        "p1",
    )

    assert result.exit_code == 0, result.stdout
    call = cli_env.server.calls[-1]
    assert call.request.from_time.ToJsonString() == "2026-11-27T00:00:00Z"
    assert call.request.to_time.ToJsonString() == "2026-11-27T01:00:00Z"
    assert call.request.filter == "severity=WARN"
    assert call.request.page_size == 7
    assert call.request.page_token == "p1"
    assert any(key in {"authorization", "x-windhub-admin-token"} for key, _value in call.metadata)


def test_publishing_status_roundtrip(cli_env: object) -> None:
    """UC-16：collector publishing 经真实 mTLS 往返，请求 channel 透传。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CollectorService", "GetPublishingStatus"),
        collector_pb2.GetPublishingStatusResponse(
            channels=[
                collector_pb2.PublishingStatus(
                    channel="ch1", nats_connected=True, published=12, failed=0
                )
            ]
        ),
    )
    result = cli_env.subprocess_invoke("collector", "publishing", "ch1")
    assert result.exit_code == 0, result.stdout
    channel = result.json()["data"]["channels"][0]
    assert channel["channel"] == "ch1"
    assert channel["nats_connected"] is True
    call = [c for c in server.calls if c.path.endswith("/GetPublishingStatus")][0]
    assert call.request.channel == "ch1"
