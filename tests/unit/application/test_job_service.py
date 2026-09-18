"""JobService 的单元测试。

验证对象：``application/job_service.py``——调度 Job 生命周期管理服务，
全部操作经 ``SchedulerPort`` 委托。

覆盖点：list_jobs / job_status（未知 → KeyError）/ pause / resume /
trigger 的委托语义与参数透传；KeyError 原样上抛（适配层据此映射 404）。

调度端口用 MagicMock——端口契约本身由
``tests/unit/scheduling/test_scheduler.py`` 对真实适配器验证。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.job_service import JobService
from wind_hub.domain.port.scheduling import JobInfo, SchedulerPort

pytestmark = pytest.mark.asyncio


def _job_info(job_id: str, paused: bool = False) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        next_run_time=None if paused else datetime.now(UTC),
        paused=paused,
    )


def _scheduler() -> MagicMock:
    sched = MagicMock(spec=SchedulerPort)
    sched.trigger_job = AsyncMock()
    return sched


async def test_list_jobs_delegates() -> None:
    sched = _scheduler()
    sched.list_jobs.return_value = [_job_info("poll:d1:default")]
    service = JobService(sched)

    jobs = await service.list_jobs()

    assert [j.job_id for j in jobs] == ["poll:d1:default"]
    sched.list_jobs.assert_called_once()


async def test_job_status_returns_info() -> None:
    sched = _scheduler()
    sched.get_job.return_value = _job_info("j1", paused=True)
    service = JobService(sched)

    info = await service.job_status("j1")

    assert info.job_id == "j1"
    assert info.paused is True
    sched.get_job.assert_called_once_with("j1")


async def test_job_status_unknown_raises_key_error() -> None:
    sched = _scheduler()
    sched.get_job.return_value = None
    service = JobService(sched)

    with pytest.raises(KeyError):
        await service.job_status("nope")


async def test_pause_and_resume_delegate() -> None:
    sched = _scheduler()
    service = JobService(sched)

    await service.pause_job("j1")
    sched.pause_job.assert_called_once_with("j1")

    await service.resume_job("j1")
    sched.resume_job.assert_called_once_with("j1")


async def test_trigger_job_delegates() -> None:
    sched = _scheduler()
    service = JobService(sched)

    await service.trigger_job("j1")
    sched.trigger_job.assert_awaited_once_with("j1")


async def test_unknown_job_errors_propagate() -> None:
    """端口对未知 job_id 抛出的 KeyError 原样上抛，不包装不吞。"""
    sched = _scheduler()
    sched.pause_job.side_effect = KeyError("nope")
    sched.resume_job.side_effect = KeyError("nope")
    sched.trigger_job.side_effect = KeyError("nope")
    service = JobService(sched)

    with pytest.raises(KeyError):
        await service.pause_job("nope")
    with pytest.raises(KeyError):
        await service.resume_job("nope")
    with pytest.raises(KeyError):
        await service.trigger_job("nope")
