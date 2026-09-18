"""GET /routes/explain — explain the routing decision for a point."""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.models import RouteExplainResponse
from wind_hub.domain.model.route import RouteDecision

router = APIRouter(tags=["routes"])


@router.get("/routes/explain", response_model=RouteExplainResponse)
async def explain_route(
    device_id: str,
    point_id: str,
) -> RouteExplainResponse:
    """Explain how a point is routed (503 when the router is absent).

    A point with no matching rule is not an error — it returns a decision with
    ``source="unmatched"`` and empty ``targets``.
    """
    ctx = get_ctx()
    if ctx.router is None:
        raise APIError("SERVICE_UNAVAILABLE", "router is not configured", status_code=503)
    decision: RouteDecision = ctx.router.explain(device_id, point_id)
    return _to_response(decision)


def _to_response(decision: RouteDecision) -> RouteExplainResponse:
    return RouteExplainResponse(
        device_id=decision.device_id,
        point_id=decision.point_id,
        targets=decision.targets,
        matched_rule=decision.matched_rule,
        source=decision.source,
    )
