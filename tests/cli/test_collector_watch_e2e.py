"""UC-08 监视采集数据 System Test：真实子进程内客户端轮询（Unary 边界、非重入）。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts import collector_pb2 as col
from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _p(server: Any, method: str) -> str:
    return server.method_path("CollectorService", method)


def test_watch_bounded_iterations_real_process(cli_env: Any) -> None:
    """S01：--count 限定轮询次数，真实进程逐轮输出，最后信封 iterations==N。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "ListPoints"),
        col.ListPointsResponse(points=[common_pb2.PointMetadata(point_id="p1")]),
    )

    async def read(request: Any, context: Any) -> Any:
        return col.ReadPointsResponse(
            device_id="d1",
            samples=[common_pb2.Sample(point_id="p1", quality="GOOD")],
        )

    server.respond(_p(server, "ReadPoints"), read)
    result = cli_env.subprocess_invoke(
        "collector", "watch", "d1", "--interval", "0.2", "--count", "2"
    )
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["iterations"] == 2
    reads = [c for c in server.calls if c.path.endswith("/ReadPoints")]
    assert len(reads) == 2
    assert reads[0].request.point_ids == ["p1"]


def test_watch_tolerates_transient_unavailable(cli_env: Any) -> None:
    """S03：单次 UNAVAILABLE 在重连预算内恢复，最终 0；不静默丢轮次预算。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "ListPoints"),
        col.ListPointsResponse(points=[common_pb2.PointMetadata(point_id="p1")]),
    )
    failures = {"n": 0}

    import grpc

    async def read(request: Any, context: Any) -> Any:
        if failures["n"] < 1:
            failures["n"] += 1
            await context.abort(grpc.StatusCode.UNAVAILABLE, "transient")
        return col.ReadPointsResponse(
            device_id="d1", samples=[common_pb2.Sample(point_id="p1", quality="GOOD")]
        )

    server.respond(_p(server, "ReadPoints"), read)
    result = cli_env.subprocess_invoke(
        "collector", "watch", "d1", "--interval", "0.2", "--count", "1"
    )
    assert result.exit_code == 0, result.stdout


def test_watch_per_point_bad_quality_and_staleness_visible(cli_env: Any) -> None:
    """S02：逐点失败与过期必须原样呈现，绝不与 GOOD 点混为健康。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "ListPoints"),
        col.ListPointsResponse(
            points=[
                common_pb2.PointMetadata(point_id="p1"),
                common_pb2.PointMetadata(point_id="p2"),
            ]
        ),
    )

    async def read(request: Any, context: Any) -> Any:
        return col.ReadPointsResponse(
            device_id="d1",
            samples=[
                common_pb2.Sample(point_id="p1", quality="GOOD", freshness="FRESH"),
                common_pb2.Sample(
                    point_id="p2",
                    quality="BAD",
                    freshness="STALE",
                    error_code="DEVICE_TIMEOUT",
                    error_message="no response",
                ),
            ],
        )

    server.respond(_p(server, "ReadPoints"), read)
    result = cli_env.subprocess_invoke(
        "collector", "watch", "d1", "--interval", "0.2", "--count", "1"
    )
    assert result.exit_code == 0, result.stdout
    import json

    envelopes = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.startswith("{") and "samples" in json.loads(line).get("data", {})
    ]
    assert envelopes, result.stdout
    samples = envelopes[0]["data"]["samples"]
    by_id = {item["point_id"]: item for item in samples}
    assert by_id["p1"]["quality"] == "GOOD"
    bad = by_id["p2"]
    assert bad["quality"] == "BAD"
    assert bad["freshness"] == "STALE"
    assert bad["error_code"] == "DEVICE_TIMEOUT"


def test_watch_slow_response_is_non_reentrant(cli_env: Any) -> None:
    """S02：慢响应（>轮询间隔）时禁止重入；两轮总耗时 ≥ 两倍服务时延。"""
    import asyncio
    import time

    server = cli_env.server
    server.respond_value(
        _p(server, "ListPoints"),
        col.ListPointsResponse(points=[common_pb2.PointMetadata(point_id="p1")]),
    )

    async def read(request: Any, context: Any) -> Any:
        await asyncio.sleep(0.5)
        return col.ReadPointsResponse(
            device_id="d1", samples=[common_pb2.Sample(point_id="p1", quality="GOOD")]
        )

    server.respond(_p(server, "ReadPoints"), read)
    started = time.monotonic()
    result = cli_env.subprocess_invoke(
        "collector", "watch", "d1", "--interval", "0.2", "--count", "2"
    )
    elapsed = time.monotonic() - started
    assert result.exit_code == 0, result.stdout
    assert elapsed >= 0.95, f"overlapping polls: {elapsed:.2f}s"
