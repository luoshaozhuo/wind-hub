"""Web API 装配根：只负责 HTTP 路由及 RPC 资源生命周期。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from secrets import compare_digest
from time import perf_counter
from uuid import uuid4

import grpc
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from web_server.rpc import RpcGateway, RpcSettings


logger = logging.getLogger(__name__)


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


class ConfigPrepareBody(BaseModel):
    """目标端根据事先部署的候选配置执行 prepare。"""

    revision_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)


class ConfigRevisionBody(BaseModel):
    """配置事务阶段使用相同的修订标识。"""

    revision_id: str = Field(min_length=1)


class WritePointBody(BaseModel):
    """要求明确命令标识与超时的设备写入请求。"""

    command_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    point_id: str = Field(min_length=1)
    value: bool | int | float | str
    timeout: float = Field(default=1.0, gt=0, le=30)


class WritePointsBody(BaseModel):
    """限制一次写入的命令数量。"""

    commands: list[WritePointBody] = Field(min_length=1, max_length=100)


class ReadPointsBody(BaseModel):
    """批量读取请求，仅声明测点 ID。"""

    point_ids: list[str] = Field(min_length=1)


def create_app(
    settings: RpcSettings | None = None, *, write_token: str | None = None
) -> FastAPI:
    """每个应用实例持有自己的 RPC 连接，测试可覆盖 gateway 依赖。"""
    config = settings or RpcSettings()

    def require_write_token(x_api_key: str | None = Header(default=None)) -> None:
        """未配置凭据时禁用设备写入。"""
        if not write_token:
            raise HTTPException(status_code=503, detail="write API is disabled")
        if x_api_key is None or not compare_digest(x_api_key, write_token):
            raise HTTPException(status_code=403, detail="write API access denied")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with RpcGateway(config) as gateway:
            app.state.gateway = gateway
            yield

    app = FastAPI(title="Wind Hub Web API", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def observe_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """记录 HTTP 状态和耗时；不记录查询参数、请求体和凭据。"""
        request_id = uuid4().hex
        started = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = (perf_counter() - started) * 1000
            logger.info(
                "http_request id=%s method=%s path=%s status=%s duration_ms=%.2f",
                request_id, request.method, request.url.path, status_code, duration_ms,
            )
            if request.method == "POST" and request.url.path.startswith("/api/v1/"):
                logger.info(
                    "control_audit id=%s operation=%s status=%s",
                    request_id, request.url.path, status_code,
                )


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

    @app.post("/api/v1/collector/tasks/{task_id}/start", tags=["collector"], dependencies=[Depends(require_write_token)])
    async def start_task(
        task_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_start_task(task_id))

    @app.post("/api/v1/collector/tasks/{task_id}/stop", tags=["collector"], dependencies=[Depends(require_write_token)])
    async def stop_task(
        task_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_stop_task(task_id))

    @app.post("/api/v1/collector/instances/{instance_id}/start", tags=["collector"], dependencies=[Depends(require_write_token)])
    async def start_instance(
        instance_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_start_instance(instance_id))

    @app.post("/api/v1/collector/instances/{instance_id}/stop", tags=["collector"], dependencies=[Depends(require_write_token)])
    async def stop_instance(
        instance_id: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_stop_instance(instance_id))

    @app.get("/api/v1/collector/sinks/{name}/verify", tags=["collector"])
    async def verify_sink(
        name: str, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_verify_sink(name))

    @app.post("/api/v1/collector/sinks/{name}/test", tags=["collector"], dependencies=[Depends(require_write_token)])
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

    @app.get("/api/v1/commander/devices/{device_id}/verify-points", tags=["commander"])
    async def verify_points(
        device_id: str,
        point_group: str = Query(default=""),
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_verify_points(device_id, point_group))


    @app.post(
        "/api/v1/commander/commands",
        tags=["commander"],
        dependencies=[Depends(require_write_token)],
    )
    async def commander_write(
        body: WritePointBody, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        return await _invoke(
            gateway.commander_write(
                body.command_id, body.device_id, body.point_id, body.value, body.timeout
            )
        )

    @app.post(
        "/api/v1/commander/commands/batch",
        tags=["commander"],
        dependencies=[Depends(require_write_token)],
    )
    async def commander_write_many(
        body: WritePointsBody, gateway: RpcGateway = Depends(get_gateway)
    ) -> dict[str, object]:
        commands = [
            (item.command_id, item.device_id, item.point_id, item.value, item.timeout)
            for item in body.commands
        ]
        return await _invoke(gateway.commander_write_many(commands))

    @app.post(
        "/api/v1/collector/config/prepare",
        tags=["collector"],
        dependencies=[Depends(require_write_token)],
    )
    async def collector_prepare_config(
        body: ConfigPrepareBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_prepare_config(body.revision_id, body.config_hash))

    @app.post(
        "/api/v1/collector/config/activate",
        tags=["collector"],
        dependencies=[Depends(require_write_token)],
    )
    async def collector_activate_config(
        body: ConfigRevisionBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_activate_config(body.revision_id))

    @app.post(
        "/api/v1/collector/config/abort",
        tags=["collector"],
        dependencies=[Depends(require_write_token)],
    )
    async def collector_abort_config(
        body: ConfigRevisionBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.collector_abort_config(body.revision_id))

    @app.post(
        "/api/v1/commander/config/prepare",
        tags=["commander"],
        dependencies=[Depends(require_write_token)],
    )
    async def commander_prepare_config(
        body: ConfigPrepareBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_prepare_config(body.revision_id, body.config_hash))

    @app.post(
        "/api/v1/commander/config/activate",
        tags=["commander"],
        dependencies=[Depends(require_write_token)],
    )
    async def commander_activate_config(
        body: ConfigRevisionBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_activate_config(body.revision_id))

    @app.post(
        "/api/v1/commander/config/abort",
        tags=["commander"],
        dependencies=[Depends(require_write_token)],
    )
    async def commander_abort_config(
        body: ConfigRevisionBody,
        gateway: RpcGateway = Depends(get_gateway),
    ) -> dict[str, object]:
        return await _invoke(gateway.commander_abort_config(body.revision_id))

    return app
