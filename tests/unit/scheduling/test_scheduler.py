"""APScheduler 调度适配器（``infra/scheduling``）的契约测试。

验证对象：:class:`APSchedulerAdapter` 对 ``SchedulerPort`` 契约的实现——
生命周期、周期 Job 注册/移除/暂停/恢复/立即触发、未知 Job 的 ``KeyError``
语义、Job 执行体参数透传。

时间相关断言一律用 ``asyncio.Event.wait`` + ``wait_for`` 超时控制（事件驱动，
非固定 sleep），避免 flaky；不能证明毫秒级调度精度（那不是本层契约）。

使用真实 APScheduler（asyncio 实现，与测试同事件循环），无外部依赖。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from wind_hub.infra.scheduling import APSchedulerAdapter

pytestmark = pytest.mark.asyncio


def _job_recorder() -> tuple[Callable[..., Awaitable[None]], asyncio.Event, list[tuple]]:
    """返回一个记录调用的 Job 执行体：每次触发把 ``args`` 记入列表并置位事件。"""

    event = asyncio.Event()
    calls: list[tuple] = []

    async def _job(*args: object) -> None:
        calls.append(args)
        event.set()

    return _job, event, calls


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_start_and_stop_toggle_running() -> None:
    sched = APSchedulerAdapter()
    assert sched.running is False

    await sched.start()
    assert sched.running is True

    await sched.stop()
    assert sched.running is False


async def test_start_is_idempotent() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    await sched.start()  # 重复启动不抛异常
    assert sched.running is True
    await sched.stop()


async def test_stop_before_start_is_noop() -> None:
    sched = APSchedulerAdapter()
    await sched.stop()  # 未启动时停止不抛异常
    assert sched.running is False


async def test_restart_allows_fresh_registration() -> None:
    """停止后重新启动，可再次注册同名 Job（旧实例已随 stop 丢弃）。"""
    sched = APSchedulerAdapter()
    await sched.start()
    job, _event, _calls = _job_recorder()
    sched.add_interval_job("j1", 60.0, job)
    await sched.stop()

    await sched.start()
    sched.add_interval_job("j1", 60.0, job)  # 同名重新注册不冲突
    assert sched.get_job("j1") is not None
    await sched.stop()


# ---------------------------------------------------------------------------
# 注册与查询
# ---------------------------------------------------------------------------


async def test_add_interval_job_registers_and_lists() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("poll:d1:default", 1.0, job, args=("d1", "default"))

        info = sched.get_job("poll:d1:default")
        assert info is not None
        assert info.job_id == "poll:d1:default"
        assert info.paused is False
        assert info.next_run_time is not None

        assert [j.job_id for j in sched.list_jobs()] == ["poll:d1:default"]
        assert sched.get_job("missing") is None
    finally:
        await sched.stop()


async def test_interval_job_fires_with_args() -> None:
    """周期 Job 按契约被触发，注册参数原样透传给执行体。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, event, calls = _job_recorder()
        sched.add_interval_job("j1", 0.05, job, args=("d1", "telemetry"))

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert calls[0] == ("d1", "telemetry")
    finally:
        await sched.stop()


async def test_add_job_replace_existing() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job)
        sched.add_interval_job("j1", 2.0, job, replace_existing=True)
        assert len(sched.list_jobs()) == 1
    finally:
        await sched.stop()


async def test_add_job_before_start_raises() -> None:
    """未启动时注册 Job 是时序错误，必须显式失败而非静默堆积。"""
    sched = APSchedulerAdapter()
    job, _event, _calls = _job_recorder()
    with pytest.raises(RuntimeError, match="not started"):
        sched.add_interval_job("j1", 1.0, job)


# ---------------------------------------------------------------------------
# 移除 / 暂停 / 恢复 / 触发
# ---------------------------------------------------------------------------


async def test_remove_job() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job)
        sched.remove_job("j1")
        assert sched.get_job("j1") is None
        assert sched.list_jobs() == []
    finally:
        await sched.stop()


async def test_pause_and_resume_job() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job)

        sched.pause_job("j1")
        info = sched.get_job("j1")
        assert info is not None
        assert info.paused is True
        assert info.next_run_time is None

        sched.resume_job("j1")
        info = sched.get_job("j1")
        assert info is not None
        assert info.paused is False
        assert info.next_run_time is not None
    finally:
        await sched.stop()


async def test_trigger_job_runs_immediately() -> None:
    """长周期 Job 被 trigger 后立即执行一次，不必等到周期点。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, event, calls = _job_recorder()
        sched.add_interval_job("j1", 600.0, job, args=("x",))

        await sched.trigger_job("j1")
        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert calls == [("x",)]
    finally:
        await sched.stop()


async def test_unknown_job_operations_raise_key_error() -> None:
    """端口契约：对未知 job_id 的操作（非查询）一律抛 KeyError。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        with pytest.raises(KeyError):
            sched.remove_job("nope")
        with pytest.raises(KeyError):
            sched.pause_job("nope")
        with pytest.raises(KeyError):
            sched.resume_job("nope")
        with pytest.raises(KeyError):
            await sched.trigger_job("nope")
    finally:
        await sched.stop()


def test_scheduler_port_contract_is_unaware_of_read_mode() -> None:
    """read_mode（如何读）与调度（何时读）正交：SchedulerPort 的公开契约
    （方法签名与 JobInfo 字段）不包含任何 read_mode 概念。"""
    import inspect

    from wind_hub.domain.port.scheduling import JobInfo, SchedulerPort

    for name in (
        "add_interval_job",
        "remove_job",
        "pause_job",
        "resume_job",
        "trigger_job",
        "get_job",
        "list_jobs",
    ):
        sig = inspect.signature(getattr(SchedulerPort, name))
        assert not any("read_mode" in p for p in sig.parameters), name
    assert "read_mode" not in JobInfo.model_fields
