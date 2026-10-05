"""GET /metrics — Prometheus metrics endpoint。"""

from __future__ import annotations

from fastapi import APIRouter, Response

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.infra import metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """从 MonitoringService 最近一次低频快照更新 gauge 并渲染 Prometheus。"""
    ctx = get_ctx()
    monitoring = ctx.monitoring
    if monitoring is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "monitoring snapshot is not configured",
            status_code=503,
        )

    try:
        status = monitoring.runtime_status()
        devices = monitoring.devices_snapshot()
        sinks = monitoring.sinks_snapshot()

        counters = monitoring.counters_snapshot()
        metrics.update_runtime_gauges(status, counters)
        metrics.update_device_gauges(
            [(row.device_id, row.protocol, row.connected) for row in devices]
        )
        metrics.update_sink_queue_depths(
            {row.name: row.queue_depth for row in sinks}
        )
        body = metrics.render()
    except Exception as exc:
        raise APIError(
            "METRICS_FAILED",
            f"failed to render metrics: {exc}",
            status_code=503,
        ) from exc

    return Response(content=body, media_type="text/plain; version=0.0.4")
