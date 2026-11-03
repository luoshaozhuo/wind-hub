"""UC-06 system tests: metrics windows, no fake zeros, P95/P99 and scoping."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from contracts import common_pb2 as common
from contracts.common_pb2 import GetMetricsResponse, Metric
from tests.collector.system.conftest import ClientsFactory
from tests.collector.system.helpers import (
    MODBUS,
    publishing,
    start_task,
    stop_task,
    wait_actual,
)
from tests.collector.system.nats_verify import collect_realtime

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


def _map(response: GetMetricsResponse) -> dict[str, Metric]:
    return {metric.name: metric for metric in response.metrics}


async def test_uc06_s01_empty_window_has_no_fabricated_zero(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _channel, config_stub, _device_stub = grpc_clients(handle.process.grpc_port)
    response = await config_stub.GetMetrics(common.GetMetricsRequest(window_ms=60000))
    names = {metric.name for metric in response.metrics}
    # Nothing observed yet: windowed counters are omitted, never reported zero.
    assert "collector.points.collected" not in names
    assert "collector.publisher.client_submitted" not in names
    assert all(metric.window_ms == 60000 for metric in response.metrics)
    assert all(
        metric.sampled_at.ToDatetime().utcoffset() is not None for metric in response.metrics
    )


async def test_uc06_s02_windowed_counts_and_p95_p99_after_real_traffic(
    collector, grpc_clients, real_nats
) -> None:
    handle = await collector(task_interval=0.2)
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    observed = await collect_realtime(real_nats.url, count=6, timeout=10)
    assert len(observed) >= 6
    assert (await publishing(device_stub)).published >= 6

    response = await config_stub.GetMetrics(common.GetMetricsRequest(window_ms=60000))
    values = _map(response)
    assert values["collector.points.collected"].value >= 6
    p95 = values["collector.publisher.latency_ms.p95"].value
    p99 = values["collector.publisher.latency_ms.p99"].value
    assert p95 >= 0.0 and p99 >= p95

    # Device scope only contains that device's metrics.
    scoped = await config_stub.GetMetrics(
        common.GetMetricsRequest(scope=f"device:{MODBUS}", window_ms=60000)
    )
    assert scoped.metrics
    assert all(f".{MODBUS}." in metric.name for metric in scoped.metrics)

    await stop_task(device_stub, config_stub, MODBUS)


async def test_uc06_s03_window_must_be_whole_seconds(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    import grpc

    handle = await collector()
    _channel, config_stub, _device_stub = grpc_clients(handle.process.grpc_port)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetMetrics(common.GetMetricsRequest(window_ms=60500))
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.GetMetrics(common.GetMetricsRequest(window_ms=12000))
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
