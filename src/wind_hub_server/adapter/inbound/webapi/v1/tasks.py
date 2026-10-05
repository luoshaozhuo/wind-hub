"""Admin API v1 Tasks 路由：聚合查询、实例查询与 start/stop。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    TaskInstanceResponse,
    TaskPageResponse,
    TaskResponse,
)
from wind_hub_server.application.task.collector import TaskWorkerUnavailableError
from wind_hub_server.application.task.placement import TaskPlacementError
from wind_hub_server.application.task.reconcile import TaskPlacementUnsafeError

router = APIRouter()


@router.get("/tasks", response_model=TaskPageResponse, tags=["v1-tasks"])
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None),
) -> TaskPageResponse:
    """分页查询 Task Definition 与实例聚合运行状态。"""
    rows = common.tasks().list_task_summaries()
    query = (search or "").strip().lower()
    if query:
        rows = [
            row
            for row in rows
            if any(
                query in str(value or "").lower()
                for value in (row.task_id, row.device, row.device_group, row.point_group)
            )
        ]
    rows = sorted(rows, key=lambda row: row.task_id)
    paged, meta = common.page(rows, page, page_size)
    return TaskPageResponse(
        items=[common.task_response(row) for row in paged],
        page=meta,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse, tags=["v1-tasks"])
async def get_task(task_id: str) -> TaskResponse:
    """查询单个 Task 的聚合运行状态。"""
    try:
        row = common.tasks().get_task_summary(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    return common.task_response(row)


@router.get(
    "/tasks/{task_id}/instances",
    response_model=list[TaskInstanceResponse],
    tags=["v1-tasks"],
)
async def list_task_instances(task_id: str) -> list[TaskInstanceResponse]:
    """查询指定 Task 展开的实例。"""
    try:
        rows = await common.tasks().list_task_instances(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    return [common.instance_response(row) for row in rows]


@router.post("/tasks/{task_id}/start", response_model=TaskResponse, tags=["v1-tasks"])
async def start_task(task_id: str) -> TaskResponse:
    """启动 Task 的全部实例；禁用 Task 返回 409。"""
    try:
        row = await common.tasks().start_task(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    except TaskPlacementError as exc:
        raise APIError("TASK_UNASSIGNED", str(exc), 409) from exc
    except TaskPlacementUnsafeError as exc:
        raise APIError("PLACEMENT_UNSAFE", str(exc), 503) from exc
    except TaskWorkerUnavailableError as exc:
        raise APIError("WORKER_UNAVAILABLE", str(exc), 503) from exc
    except ValueError as exc:
        raise APIError("TASK_DISABLED", str(exc), 409) from exc
    return common.task_response(row)


@router.post("/tasks/{task_id}/stop", response_model=TaskResponse, tags=["v1-tasks"])
async def stop_task(task_id: str) -> TaskResponse:
    """停止 Task 的全部实例；不停止 Runtime 或设备连接。"""
    try:
        row = await common.tasks().stop_task(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    except TaskPlacementError as exc:
        raise APIError("TASK_UNASSIGNED", str(exc), 409) from exc
    except TaskWorkerUnavailableError as exc:
        raise APIError("WORKER_UNAVAILABLE", str(exc), 503) from exc
    return common.task_response(row)
