"""Web API 装配根：只负责 HTTP 路由及 RPC 资源生命周期。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager

import grpc
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from web_server.rpc import RpcGateway, RpcSettings


def get_gateway(request: Request) -> RpcGateway:
    """取出当前应用的 RPC gateway，不使用进程全局状态。"""
    gateway: RpcGateway = request.app.state.gateway
    return gateway


def _http_error(error: grpc.aio.AioRpcError) -> HTTPException:
    """保留 RPC status，禁止把不可用上游伪装成空结果。"""
    status = error.code()
    codes = {
        grpc.StatusCode.INVALID_ARGUMENT: 400,
        grpc.StatusCode.NOT_FOUND: 404,
        grpc.StatusCode.ALREADY_EXISTS: 409,
        grpc.StatusCode.FAILED_PRECONDITION: 409,
        grpc.StatusCode.DEADLINE_EXCEEDED: 504,
        grpc.StatusCode.UNAVAILABLE: 503,
    }
    return HTTPException(
        status_code=codes.get(status, 502),
        detail={"code": status.name, "message": error.details() or ""},
    )


async def _invoke(operation: Awaitable[dict[str, object]]) -> dict[str, object]:
    """统一异常转换；operation 是已创建的 awaitable RPC 调用。"""
    try:
        result: dict[str, object] = await operation
    except grpc.aio.AioRpcError as exc:
        raise _http_error(exc) from exc
    return result


class ReadPointsBody(BaseModel):
    """批量读取请求，仅声明测点 ID。"""

    point_ids: list[str] = Field(min_length=1)


def create_app(settings: RpcSettings | None = None) -> FastAPI:
    """每个应用实例持有自己的 RPC 连接，测试可覆盖 gateway 依赖。"""
    config = settings or RpcSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with RpcGateway(config) as gateway:
            app.state.gateway = gateway
            yield

    app = FastAPI(title="Wind Hub Web API", version="1.0.0", lifespan=lifespan)

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/collector/info", tags=["collector"])
    async def collector_info(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_info())

    @app.get("/api/v1/collector/status", tags=["collector"])
    async def collector_status(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_status())

    @app.get("/api/v1/collector/metrics", tags=["collector"])
    async def collector_metrics(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_metrics())

    @app.get("/api/v1/collector/devices", tags=["collector"])
    async def collector_devices(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_devices())

    @app.get("/api/v1/collector/sinks", tags=["collector"])
    async def collector_sinks(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_sinks())

    @app.get("/api/v1/collector/tasks", tags=["collector"])
    async def collector_tasks(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_tasks())

    @app.get("/api/v1/collector/instances", tags=["collector"])
    async def collector_instances(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.collector_instances())

    @app.get("/api/v1/collector/latest-values", tags=["collector"])
    async def collector_latest_values(
        device_id: str = "",
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_latest_values(device_id))

    @app.get("/api/v1/commander/status", tags=["commander"])
    async def commander_status(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.commander_status())

    @app.get("/api/v1/commander/devices", tags=["commander"])
    async def commander_devices(gateway: RpcGateway = Depends(get_gateway)) -> dict[str, object]:
        return await _invoke(gateway.commander_devices())

    @app.get("/api/v1/commander/devices/{device_id}/points/{point_id}", tags=["commander"])
    async def commander_read(
        device_id: str,
        point_id: str,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_read(device_id, point_id))

    @app.get("/api/v1/commander/devices/{device_id}/verify", tags=["commander"])
    async def commander_verify(
        device_id: str,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_verify_device(device_id))


    @app.get("/api/v1/collector/tasks/{task_id}", tags=["collector"])
    async def collector_task(
        task_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_task(task_id))

    @app.get("/api/v1/collector/instances/{instance_id}", tags=["collector"])
    async def collector_instance(
        instance_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_instance(instance_id))

    @app.post("/api/v1/collector/tasks/{task_id}/start", tags=["collector"])
    async def start_task(
        task_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_start_task(task_id))

    @app.post("/api/v1/collector/tasks/{task_id}/stop", tags=["collector"])
    async def stop_task(
        task_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_stop_task(task_id))

    @app.post("/api/v1/collector/instances/{instance_id}/start", tags=["collector"])
    async def start_instance(
        instance_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_start_instance(instance_id))

    @app.post("/api/v1/collector/instances/{instance_id}/stop", tags=["collector"])
    async def stop_instance(
        instance_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_stop_instance(instance_id))

    @app.get("/api/v1/collector/sinks/{name}/verify", tags=["collector"])
    async def verify_sink(
        name: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_verify_sink(name))

    @app.post("/api/v1/collector/sinks/{name}/test", tags=["collector"])
    async def test_sink(
        name: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_test_sink(name))

    @app.post("/api/v1/commander/devices/{device_id}/read-many", tags=["commander"])
    async def read_many(
        device_id: str,
        body: ReadPointsBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_read_many(device_id, body.point_ids))

    @app.get(
        "/api/v1/commander/devices/{device_id}/points/{point_id}/verify",
        tags=["commander"],
    )
    async def verify_point(
        device_id: str,
        point_id: str,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_verify_point(device_id, point_id))

    @app.get(
        "/api/v1/commander/devices/{device_id}/points/{point_id}/resolve",
        tags=["commander"],
    )
    async def resolve_point(
        device_id: str,
        point_id: str,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_resolve_point(device_id, point_id))

    @app.get("/api/v1/commander/devices/{device_id}/points/verify", tags=["commander"])
    async def verify_points(
        device_id: str,
        point_group: str = Query(default=""),
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_verify_points(device_id, point_group))

    return app
