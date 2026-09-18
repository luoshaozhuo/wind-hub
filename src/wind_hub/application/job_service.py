"""Job service — 调度 Job 运行控制的应用服务。

将 :class:`~wind_hub.domain.port.scheduling.SchedulerPort` 包装为
:class:`~wind_hub.domain.port.inbound.JobUseCase`，供上层管理「调度 Job
生命周期」：暂停/恢复、立即触发、状态查询与列表。

职责边界：本服务只管 Job 粒度操作。Runtime 整体 ``start()``/``stop()``
（Runtime 生命周期）与进程启停（process 生命周期）是另外两层语义，不在
此暴露——三者不得混用。
"""

from __future__ import annotations

from wind_hub.domain.port.inbound import JobUseCase
from wind_hub.domain.port.scheduling import JobInfo, SchedulerPort


class JobService(JobUseCase):
    """调度 Job 管理服务——全部操作直接委托给 SchedulerPort。

    未知 ``job_id`` 的 ``KeyError`` 由端口契约原样上抛，调用方（适配层）
    据此映射为 404 / 非零退出。
    """

    def __init__(self, scheduler: SchedulerPort) -> None:
        self._scheduler = scheduler

    async def list_jobs(self) -> list[JobInfo]:
        """返回当前全部调度 Job 的快照。"""
        return self._scheduler.list_jobs()

    async def job_status(self, job_id: str) -> JobInfo:
        """返回单个 Job 的快照。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        info = self._scheduler.get_job(job_id)
        if info is None:
            raise KeyError(job_id)
        return info

    async def pause_job(self, job_id: str) -> None:
        """暂停 Job——保留定义但不再触发。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        self._scheduler.pause_job(job_id)

    async def resume_job(self, job_id: str) -> None:
        """恢复已暂停的 Job。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        self._scheduler.resume_job(job_id)

    async def trigger_job(self, job_id: str) -> None:
        """立即触发一次 Job（周期计划不变）。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        await self._scheduler.trigger_job(job_id)
