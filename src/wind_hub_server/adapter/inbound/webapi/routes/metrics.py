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

        metrics.update_gauges(
            devices_total_val=int(status.get("device_count") or 0),
            devices_connected_val=int(status.get("devices_connected") or 0),
            sinks_total_val=int(status.get("sink_count") or 0),
            sinks_healthy_val=int(status.get("sinks_healthy") or 0),
        )
        metrics.update_device_gauges(
            [
                (
                    str(row.get("device_id") or ""),
                    str(row.get("protocol") or ""),
                    bool(row.get("connected")),
                )
                for row in devices
            ]
        )
        queue_depths: dict[str, int] = {}
        for row in sinks:
            sink_name = row.get("name")
            depth = row.get("queue_depth")
            if sink_name is None or not isinstance(depth, int):
                continue
            queue_depths[str(sink_name)] = depth
        metrics.update_sink_queue_depths(queue_depths)
        body = metrics.render()
    except Exception as exc:
        raise APIError(
            "METRICS_FAILED",
            f"failed to render metrics: {exc}",
            status_code=503,
        ) from exc

    return Response(content=body, media_type="text/plain; version=0.0.4")
