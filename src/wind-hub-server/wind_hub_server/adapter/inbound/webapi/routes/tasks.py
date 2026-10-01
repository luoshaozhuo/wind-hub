"""``/tasks`` — 采集 Task / Task Instance 生命周期管理。

路由只经 ``tasks``（:class:`~wind_hub.application.usecase.task.TaskUseCase`）
操作实例生命周期，不直接访问 Runtime。start/stop 幂等；未知
``instance_id`` 统一映射为 404。
"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.models import (
    TaskBatchResponse,
    TaskInstanceResponse,
    TaskResponse,
)
from wind_hub.application.usecase.task import (
    TaskBatchResult,
    TaskDetail,
    TaskInstanceDetail,
    TaskUseCase,
)

router = APIRouter(tags=["tasks"])


def _tasks() -> TaskUseCase:
    ctx = get_ctx()
    if ctx.tasks is None:
        raise APIError("SERVICE_UNAVAILABLE", "tasks use case is not configured", status_code=503)
    return ctx.tasks


def _to_task_response(task: TaskDetail) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        device=task.device,
        device_group=task.device_group,
        point_group=task.point_group,
        interval=task.interval,
        targets=task.targets,
        enabled=task.enabled,
    )


def _to_instance_response(inst: TaskInstanceDetail) -> TaskInstanceResponse:
    return TaskInstanceResponse(
        instance_id=inst.instance_id,
        task_id=inst.task_id,
        device_id=inst.device_id,
        point_group=inst.point_group,
        interval=inst.interval,
        targets=inst.targets,
        state=inst.state.value,
    )


def _to_batch_response(result: TaskBatchResult) -> TaskBatchResponse:
    return TaskBatchResponse(
        total=result.total,
        changed=result.changed,
        unchanged=result.unchanged,
    )


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks() -> list[TaskResponse]:
    """List all collection task definitions (tasks.yaml)."""
    tasks = await _tasks().list_tasks()
    return [_to_task_response(task) for task in tasks]


@router.post("/tasks/start-all", response_model=TaskBatchResponse)
async def start_all_instances() -> TaskBatchResponse:
    """Start periodic collection of all task instances (idempotent)."""
    return _to_batch_response(await _tasks().start_all_instances())


@router.post("/tasks/stop-all", response_model=TaskBatchResponse)
async def stop_all_instances() -> TaskBatchResponse:
    """Stop periodic collection of all task instances (idempotent).

    Only collection is stopped — the runtime, device connections
    and sinks keep running.
    """
    return _to_batch_response(await _tasks().stop_all_instances())


@router.get("/tasks/instances", response_model=list[TaskInstanceResponse])
async def list_instances() -> list[TaskInstanceResponse]:
    """List all task instances and their lifecycle state."""
    instances = await _tasks().list_instances()
    return [_to_instance_response(inst) for inst in instances]


@router.get("/tasks/instances/{instance_id}", response_model=TaskInstanceResponse)
async def get_instance(instance_id: str) -> TaskInstanceResponse:
    """Return the state of a single task instance (404 when unknown)."""
    try:
        inst = await _tasks().get_instance(instance_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown task instance '{instance_id}'", status_code=404
        ) from None
    return _to_instance_response(inst)


@router.post("/tasks/instances/{instance_id}/start", response_model=TaskInstanceResponse)
async def start_instance(instance_id: str) -> TaskInstanceResponse:
    """Start periodic collection of a task instance (idempotent; 404 when unknown)."""
    try:
        inst = await _tasks().start_instance(instance_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown task instance '{instance_id}'", status_code=404
        ) from None
    return _to_instance_response(inst)


@router.post("/tasks/instances/{instance_id}/stop", response_model=TaskInstanceResponse)
async def stop_instance(instance_id: str) -> TaskInstanceResponse:
    """Stop periodic collection of a task instance (idempotent; 404 when unknown)."""
    try:
        inst = await _tasks().stop_instance(instance_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown task instance '{instance_id}'", status_code=404
        ) from None
    return _to_instance_response(inst)
