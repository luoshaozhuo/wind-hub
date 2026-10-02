"""GET /metrics — Prometheus metrics endpoint。"""

from __future__ import annotations

from fastapi import APIRouter, Response

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.infra import metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """从 Collector RPC 快照更新 gauge 后渲染 Prometheus 文本。"""
    ctx = get_ctx()
    query = ctx.query
    if query is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "worker query use case is not configured",
            status_code=503,
        )

    try:
        status = await query.status()
        devices = await query.list_devices()
        sinks = await query.list_sinks()

        metrics.update_gauges(
            devices_total_val=status.device_count,
            devices_connected_val=status.devices_connected,
            sinks_total_val=status.sink_count,
            sinks_healthy_val=status.sinks_healthy,
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
        metrics.update_sink_queue_depths(
            {
                str(row.get("name") or ""): int(row.get("queue_depth") or 0)
                for row in sinks
                if row.get("name") is not None
            }
        )
        body = metrics.render()
    except Exception as exc:
        raise APIError(
            "METRICS_FAILED",
            f"failed to render metrics: {exc}",
            status_code=503,
        ) from exc

    return Response(content=body, media_type="text/plain; version=0.0.4")
