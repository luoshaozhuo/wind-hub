"""JobUseCase 的单元测试。

验证对象：``application/usecase/job.py``——采集 Job 显式生命周期控制用例，
全部调度状态操作经 ``SchedulerPort`` 委托。

覆盖点：

- list_jobs / get_job（未知 → KeyError）与展示级补全（device/group 取自
  ``JobMetadata``、state 二态视图、interval 透传）；
- start_job / stop_job 的 resume/pause 委托与幂等（已在目标状态不触碰）；
- start_all_jobs / stop_all_jobs 只匹配 ``metadata.kind == "poll"`` 的
  采集 Job——哪怕 job_id 形似 ``poll:{a}:{b}``，分类也只由元数据决定；
- KeyError 原样上抛（适配层据此映射 404）。

调度端口用 MagicMock——端口契约本身由
``tests/unit/scheduling/test_scheduler.py`` 对真实适配器验证。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

import wind_hub.application.usecase.job as job_module
from wind_hub.application.port.scheduling import JobInfo, JobMetadata, JobState, SchedulerPort
from wind_hub.application.usecase.job import JobUseCase

pytestmark = pytest.mark.asyncio


def _poll_meta(device_id: str, group: str) -> JobMetadata:
    return JobMetadata(kind="poll", device_id=device_id, group=group)


def _job_info(
    job_id: str,
    metadata: JobMetadata,
    paused: bool = False,
    interval: float = 1.0,
) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        metadata=metadata,
        next_run_time=None if paused else datetime.now(UTC),
        paused=paused,
        interval_seconds=interval,
    )


def _scheduler(*jobs: JobInfo) -> MagicMock:
    sched = MagicMock(spec=SchedulerPort)
    by_id = {j.job_id: j for j in jobs}
    sched.get_job.side_effect = by_id.get
    sched.list_jobs.side_effect = lambda: list(by_id.values())

    def _pause(job_id: str) -> None:
        if job_id not in by_id:
            raise KeyError(job_id)
        current = by_id[job_id]
        by_id[job_id] = _job_info(
            job_id, current.metadata, paused=True, interval=current.interval_seconds
        )

    def _resume(job_id: str) -> None:
        if job_id not in by_id:
            raise KeyError(job_id)
        current = by_id[job_id]
        by_id[job_id] = _job_info(
            job_id, current.metadata, paused=False, interval=current.interval_seconds
        )

    sched.pause_job.side_effect = _pause
    sched.resume_job.side_effect = _resume
    return sched


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def test_list_jobs_returns_enriched_details() -> None:
    sched = _scheduler(
        _job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=True, interval=2.0)
    )
    usecase = JobUseCase(sched)

    jobs = await usecase.list_jobs()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_id == "poll:d1:fast"
    assert job.device_id == "d1"
    assert job.group == "fast"
    assert job.interval_seconds == 2.0
    assert job.state is JobState.STOPPED
    assert job.next_run_time is None


async def test_list_jobs_running_state_and_next_run_time() -> None:
    sched = _scheduler(_job_info("poll:d1:slow", _poll_meta("d1", "slow"), paused=False))
    usecase = JobUseCase(sched)

    job = (await usecase.list_jobs())[0]

    assert job.state is JobState.RUNNING
    assert job.next_run_time is not None


async def test_list_jobs_non_poll_job_has_no_device_group() -> None:
    """非采集 Job（``kind != "poll"``）无 device/group 归属，但仍可列出。

    job_id 刻意写成三段冒号分隔、形似 ``poll:{device}:{group}``——证明
    展示字段只来自元数据，与 id 形状无关。
    """
    sched = _scheduler(_job_info("abc:def:ghi", JobMetadata(kind="system")))
    usecase = JobUseCase(sched)

    job = (await usecase.list_jobs())[0]

    assert job.device_id is None
    assert job.group is None


async def test_device_id_with_colons_roundtrip_from_metadata() -> None:
    """device_id 含冒号（``site:a:wtg-001``）时展示字段仍完整正确——

    Job ID ``poll:site:a:wtg-001:fast`` 按 ``:`` 拆不出正确归属；只有
    元数据显式携带才能还原。
    """
    sched = _scheduler(
        _job_info(
            "poll:site:a:wtg-001:fast",
            _poll_meta("site:a:wtg-001", "fast"),
        )
    )
    usecase = JobUseCase(sched)

    job = await usecase.get_job("poll:site:a:wtg-001:fast")

    assert job.device_id == "site:a:wtg-001"
    assert job.group == "fast"


async def test_get_job_unknown_raises_key_error() -> None:
    sched = _scheduler()
    usecase = JobUseCase(sched)

    with pytest.raises(KeyError):
        await usecase.get_job("poll:nope:fast")


def test_job_usecase_does_not_parse_job_id() -> None:
    """结构保证：JobUseCase 源码不存在任何 job_id 字符串解析——

    无 ``split``/``startswith``，业务字段一律来自 ``JobMetadata``。
    """
    source = inspect.getsource(job_module)
    assert ".split(" not in source
    assert "startswith" not in source


# ---------------------------------------------------------------------------
# start / stop 单任务
# ---------------------------------------------------------------------------


async def test_start_job_resumes_stopped_job() -> None:
    sched = _scheduler(_job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=True))
    usecase = JobUseCase(sched)

    job = await usecase.start_job("poll:d1:fast")

    sched.resume_job.assert_called_once_with("poll:d1:fast")
    assert job.state is JobState.RUNNING


async def test_start_job_is_idempotent_when_running() -> None:
    sched = _scheduler(_job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=False))
    usecase = JobUseCase(sched)

    job = await usecase.start_job("poll:d1:fast")

    sched.resume_job.assert_not_called()
    assert job.state is JobState.RUNNING


async def test_stop_job_pauses_running_job() -> None:
    sched = _scheduler(_job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=False))
    usecase = JobUseCase(sched)

    job = await usecase.stop_job("poll:d1:fast")

    sched.pause_job.assert_called_once_with("poll:d1:fast")
    assert job.state is JobState.STOPPED


async def test_stop_job_is_idempotent_when_stopped() -> None:
    sched = _scheduler(_job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=True))
    usecase = JobUseCase(sched)

    job = await usecase.stop_job("poll:d1:fast")

    sched.pause_job.assert_not_called()
    assert job.state is JobState.STOPPED


async def test_start_stop_unknown_job_raises_key_error() -> None:
    sched = _scheduler()
    usecase = JobUseCase(sched)

    with pytest.raises(KeyError):
        await usecase.start_job("poll:nope:fast")
    with pytest.raises(KeyError):
        await usecase.stop_job("poll:nope:fast")
    sched.resume_job.assert_not_called()
    sched.pause_job.assert_not_called()


async def test_stop_one_group_keeps_sibling_jobs_untouched() -> None:
    """stop 单个 group 不影响同设备的其他 Job。"""
    sched = _scheduler(
        _job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=False),
        _job_info("poll:d1:slow", _poll_meta("d1", "slow"), paused=False),
    )
    usecase = JobUseCase(sched)

    await usecase.stop_job("poll:d1:fast")

    assert (await usecase.get_job("poll:d1:fast")).state is JobState.STOPPED
    assert (await usecase.get_job("poll:d1:slow")).state is JobState.RUNNING


# ---------------------------------------------------------------------------
# 批量启停——按 metadata.kind == "poll" 圈定范围
# ---------------------------------------------------------------------------


async def test_start_all_jobs_only_touches_stopped_poll_jobs() -> None:
    sched = _scheduler(
        _job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=True),
        _job_info("poll:d1:slow", _poll_meta("d1", "slow"), paused=True),
        _job_info("poll:d2:default", _poll_meta("d2", "default"), paused=False),
        # job_id 形似采集 Job，但 kind=system——不在批量范围内
        _job_info("abc:def:ghi", JobMetadata(kind="system"), paused=True),
    )
    usecase = JobUseCase(sched)

    result = await usecase.start_all_jobs()

    assert result.total == 3
    assert result.changed == 2
    assert result.unchanged == 1
    # 系统 Job 不在批量范围内
    assert sched.get_job("abc:def:ghi").paused is True
    assert (await usecase.get_job("poll:d1:fast")).state is JobState.RUNNING
    assert (await usecase.get_job("poll:d1:slow")).state is JobState.RUNNING


async def test_stop_all_jobs_only_touches_running_poll_jobs() -> None:
    sched = _scheduler(
        _job_info("poll:d1:fast", _poll_meta("d1", "fast"), paused=False),
        _job_info("poll:d1:slow", _poll_meta("d1", "slow"), paused=True),
        _job_info("abc:def:ghi", JobMetadata(kind="system"), paused=False),
    )
    usecase = JobUseCase(sched)

    result = await usecase.stop_all_jobs()

    assert result.total == 2
    assert result.changed == 1
    assert result.unchanged == 1
    assert sched.get_job("abc:def:ghi").paused is False
    assert (await usecase.get_job("poll:d1:fast")).state is JobState.STOPPED


async def test_batch_operations_classify_by_kind_not_job_id() -> None:
    """分类只由 ``metadata.kind`` 决定：

    - job_id 带 ``poll:`` 前缀但 ``kind="system"`` → 批量操作不触碰；
    - job_id 毫无 ``poll`` 字样但 ``kind="poll"`` → 正常纳入批量范围。
    """
    sched = _scheduler(
        _job_info("poll:fake:fast", JobMetadata(kind="system"), paused=True),
        _job_info("no-prefix-at-all", _poll_meta("d9", "slow"), paused=True),
    )
    usecase = JobUseCase(sched)

    result = await usecase.start_all_jobs()

    assert result.total == 1
    assert result.changed == 1
    assert sched.get_job("poll:fake:fast").paused is True  # 未被启动
    assert sched.get_job("no-prefix-at-all").paused is False  # 已被启动


async def test_start_all_jobs_with_no_jobs_returns_zero_summary() -> None:
    usecase = JobUseCase(_scheduler())

    result = await usecase.start_all_jobs()

    assert result.total == 0
    assert result.changed == 0
    assert result.unchanged == 0
