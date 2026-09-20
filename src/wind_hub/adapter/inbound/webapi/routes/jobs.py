"""``/jobs`` — 采集 Job 生命周期管理。

路由只经 ``job_service``（:class:`~wind_hub.domain.port.inbound.JobUseCase`）
操作调度状态，不直接访问调度适配器。start/stop 是 resume/pause 语义且幂等；
未知 ``job_id`` 统一映射为 404。

路由顺序注意：``/jobs/start-all`` 与 ``/jobs/stop-all`` 必须先于
``/jobs/{job_id}`` 声明，否则会被路径参数捕获。
"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.models import JobBatchResponse, JobResponse
from wind_hub.domain.port.inbound import JobBatchResult, JobDetail, JobUseCase

router = APIRouter(tags=["jobs"])


def _service() -> JobUseCase:
    ctx = get_ctx()
    if ctx.job_service is None:
        raise APIError("SERVICE_UNAVAILABLE", "job_service is not configured", status_code=503)
    return ctx.job_service


def _to_response(job: JobDetail) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        device_id=job.device_id,
        group=job.group,
        interval=job.interval_seconds,
        state=job.state.value,
        next_run_time=job.next_run_time,
    )


def _to_batch_response(result: JobBatchResult) -> JobBatchResponse:
    return JobBatchResponse(
        total=result.total,
        changed=result.changed,
        unchanged=result.unchanged,
    )


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs() -> list[JobResponse]:
    """List all scheduled jobs and their lifecycle state."""
    jobs = await _service().list_jobs()
    return [_to_response(job) for job in jobs]


@router.post("/jobs/start-all", response_model=JobBatchResponse)
async def start_all_jobs() -> JobBatchResponse:
    """Start periodic scheduling of all acquisition jobs (idempotent)."""
    return _to_batch_response(await _service().start_all_jobs())


@router.post("/jobs/stop-all", response_model=JobBatchResponse)
async def stop_all_jobs() -> JobBatchResponse:
    """Stop periodic scheduling of all acquisition jobs (idempotent).

    Only acquisition scheduling is paused — the runtime, device connections
    and sinks keep running.
    """
    return _to_batch_response(await _service().stop_all_jobs())


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    """Return the state of a single job (404 when unknown)."""
    try:
        job = await _service().get_job(job_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown job '{job_id}'", status_code=404) from None
    return _to_response(job)


@router.post("/jobs/{job_id}/start", response_model=JobResponse)
async def start_job(job_id: str) -> JobResponse:
    """Start periodic scheduling of a job (idempotent; 404 when unknown)."""
    try:
        job = await _service().start_job(job_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown job '{job_id}'", status_code=404) from None
    return _to_response(job)


@router.post("/jobs/{job_id}/stop", response_model=JobResponse)
async def stop_job(job_id: str) -> JobResponse:
    """Stop periodic scheduling of a job (idempotent; 404 when unknown)."""
    try:
        job = await _service().stop_job(job_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown job '{job_id}'", status_code=404) from None
    return _to_response(job)
