"""GET /metrics — Prometheus metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.infra import metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Return the current engine metrics in Prometheus text format.

    从 ``AppContext.runtime`` 读取快照，覆盖四个 gauge（设备/连接数、
    sink/健康数），再渲染 Prometheus 文本。计数器（如
    ``points_collected_total``）由采集循环经组合根注入的回调累加，这里只负责
    拉取。
    """
    ctx = get_ctx()
    runtime = ctx.runtime
    if runtime is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "runtime is not configured",
            status_code=503,
        )

    try:
        # Runtime.health() 返回「设备优先、随后 sink」的合并字典（见其 docstring），
        # 因此按 device_count 切分即可区分两类组件。
        health = list(runtime.health().values())
        device_health = health[: runtime.device_count]
        sink_health = health[runtime.device_count :]

        devices_connected = sum(1 for h in device_health if h.healthy)
        sinks_healthy = sum(1 for h in sink_health if h.healthy)

        metrics.update_gauges(
            devices_total_val=runtime.device_count,
            devices_connected_val=devices_connected,
            sinks_total_val=runtime.sink_count,
            sinks_healthy_val=sinks_healthy,
        )
        # 逐设备连通 gauge（标签 device_id/protocol，取自配置，基数受控）：
        # connected 以驱动实时 health 为准（与 QueryService 口径一致）。
        metrics.update_device_gauges(
            [
                (
                    device_id,
                    cfg.protocol,
                    runtime.protocols[device_id].health().healthy
                    if device_id in runtime.protocols
                    else False,
                )
                for device_id, cfg in runtime.devices.items()
            ]
        )
        # sink 队列深度 gauge（队列归 Runtime 所有）。
        metrics.update_sink_queue_depths(runtime.sink_queue_depths())
        body = metrics.render()
    except Exception as exc:
        raise APIError(
            "METRICS_FAILED",
            f"failed to render metrics: {exc}",
            status_code=500,
        ) from exc

    return Response(content=body, media_type="text/plain; version=0.0.4")
