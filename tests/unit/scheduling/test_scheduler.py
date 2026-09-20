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

from wind_hub.domain.port.scheduling import JobMetadata
from wind_hub.infra.scheduling import APSchedulerAdapter

pytestmark = pytest.mark.asyncio


def _meta(device_id: str = "d1", group: str = "fast", kind: str = "poll") -> JobMetadata:
    """构造测试用 Job 业务元数据（默认一个采集 Job 的归属）。"""
    return JobMetadata(kind=kind, device_id=device_id, group=group)


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
    sched.add_interval_job("j1", 60.0, job, metadata=_meta())
    await sched.stop()

    await sched.start()
    sched.add_interval_job("j1", 60.0, job, metadata=_meta())  # 同名重新注册不冲突
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
        sched.add_interval_job(
            "poll:d1:default", 1.0, job, metadata=_meta(), args=("d1", "default")
        )

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
        sched.add_interval_job("j1", 0.05, job, metadata=_meta(), args=("d1", "telemetry"))

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert calls[0] == ("d1", "telemetry")
    finally:
        await sched.stop()


async def test_add_job_replace_existing() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job, metadata=_meta())
        sched.add_interval_job("j1", 2.0, job, metadata=_meta(), replace_existing=True)
        assert len(sched.list_jobs()) == 1
    finally:
        await sched.stop()


async def test_add_interval_job_start_paused_registers_stopped() -> None:
    """``start_paused=True``：原子注册暂停态 Job——``next_run_time=None``、
    状态 STOPPED，且注册到恢复之间不会触发任何执行。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, event, calls = _job_recorder()
        sched.add_interval_job("poll:d1:fast", 0.05, job, metadata=_meta(), start_paused=True)

        info = sched.get_job("poll:d1:fast")
        assert info is not None
        assert info.paused is True
        assert info.next_run_time is None
        assert info.state.value == "stopped"
        assert info.interval_seconds == 0.05

        # 等待远超一个周期——暂停的 Job 不得触发
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=0.3)
        assert calls == []
    finally:
        await sched.stop()


async def test_resume_of_paused_registered_job_schedules_normally() -> None:
    """paused 注册的 Job 经 resume 恢复正常周期调度——不立即补跑一次，
    从恢复时刻起按 interval 触发。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, event, calls = _job_recorder()
        sched.add_interval_job("j1", 0.05, job, metadata=_meta(), start_paused=True)
        assert calls == []

        sched.resume_job("j1")
        info = sched.get_job("j1")
        assert info is not None
        assert info.paused is False
        assert info.next_run_time is not None

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert len(calls) >= 1
    finally:
        await sched.stop()


async def test_replace_existing_can_preserve_stopped_state() -> None:
    """同 id 重建（interval 变更）经 ``start_paused`` 保持原暂停态。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job, metadata=_meta(), start_paused=True)
        sched.add_interval_job(
            "j1", 2.0, job, metadata=_meta(), replace_existing=True, start_paused=True
        )
        info = sched.get_job("j1")
        assert info is not None
        assert info.paused is True
        assert info.interval_seconds == 2.0
    finally:
        await sched.stop()


async def test_add_job_before_start_raises() -> None:
    """未启动时注册 Job 是时序错误，必须显式失败而非静默堆积。"""
    sched = APSchedulerAdapter()
    job, _event, _calls = _job_recorder()
    with pytest.raises(RuntimeError, match="not started"):
        sched.add_interval_job("j1", 1.0, job, metadata=_meta())


# ---------------------------------------------------------------------------
# 移除 / 暂停 / 恢复 / 触发
# ---------------------------------------------------------------------------


async def test_remove_job() -> None:
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job, metadata=_meta())
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
        sched.add_interval_job("j1", 1.0, job, metadata=_meta())

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
        sched.add_interval_job("j1", 600.0, job, metadata=_meta(), args=("x",))

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


# ---------------------------------------------------------------------------
# 元数据生命周期——注册写入 / 查询返回 / 替换更新 / 移除清理
# ---------------------------------------------------------------------------


async def test_get_job_returns_registered_metadata() -> None:
    """注册的 ``metadata`` 经 ``get_job`` 快照原样返回（不由 job_id 推导）。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        meta = _meta(device_id="d1", group="fast")
        # job_id 刻意与元数据字段不一致——返回值必须来自元数据而非 id 解析
        sched.add_interval_job("whatever-id", 1.0, job, metadata=meta)

        info = sched.get_job("whatever-id")
        assert info is not None
        assert info.metadata == meta
        assert info.metadata.kind == "poll"
        assert info.metadata.device_id == "d1"
        assert info.metadata.group == "fast"
    finally:
        await sched.stop()


async def test_list_jobs_returns_registered_metadata() -> None:
    """``list_jobs`` 的每个快照都携带各自的注册元数据。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job, metadata=_meta(device_id="d1", group="fast"))
        sched.add_interval_job("j2", 1.0, job, metadata=JobMetadata(kind="system"))

        by_id = {info.job_id: info for info in sched.list_jobs()}
        assert by_id["j1"].metadata == _meta(device_id="d1", group="fast")
        assert by_id["j2"].metadata == JobMetadata(kind="system")
        assert by_id["j2"].metadata.device_id is None
    finally:
        await sched.stop()


async def test_pause_resume_preserves_metadata() -> None:
    """pause/resume 只翻转调度状态，不改变元数据。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        meta = _meta()
        sched.add_interval_job("j1", 1.0, job, metadata=meta)

        sched.pause_job("j1")
        info = sched.get_job("j1")
        assert info is not None
        assert info.metadata == meta

        sched.resume_job("j1")
        info = sched.get_job("j1")
        assert info is not None
        assert info.metadata == meta
    finally:
        await sched.stop()


async def test_replace_existing_updates_metadata() -> None:
    """同 id 替换注册：元数据随新定义更新（interval 变更路径的契约）。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job, metadata=_meta(group="fast"))
        sched.add_interval_job("j1", 2.0, job, metadata=_meta(group="slow"), replace_existing=True)

        info = sched.get_job("j1")
        assert info is not None
        assert info.metadata == _meta(group="slow")
        assert info.interval_seconds == 2.0
    finally:
        await sched.stop()


async def test_remove_job_cleans_metadata() -> None:
    """移除 Job 同时清理其元数据——同名再注册不得读到残留。"""
    sched = APSchedulerAdapter()
    await sched.start()
    try:
        job, _event, _calls = _job_recorder()
        sched.add_interval_job("j1", 1.0, job, metadata=_meta())
        sched.remove_job("j1")
        assert "j1" not in sched._job_metadata

        sched.add_interval_job("j1", 1.0, job, metadata=JobMetadata(kind="system"))
        info = sched.get_job("j1")
        assert info is not None
        assert info.metadata == JobMetadata(kind="system")
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
