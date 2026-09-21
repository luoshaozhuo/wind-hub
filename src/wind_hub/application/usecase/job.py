"""Job use case——采集 Job 显式生命周期控制的应用编排。

基于 :class:`~wind_hub.application.port.scheduling.SchedulerPort`，供
CLI / Web API 管理「调度 Job 生命周期」：查询、start（resume）、
stop（pause）、批量启停。

核心语义：

- 配置决定「有哪些 Job」——Job 的创建与删除只发生在启动装配与配置热
  重载；本用例的 start/stop 只翻转调度状态，不增删 Job；
- Job 状态只有 ``RUNNING`` / ``STOPPED`` 两态——采集执行状态
  （failed/partial）与设备连接状态（disconnected）是另外的维度，
  不由本用例呈现或修改；
- 批量操作只匹配 ``JobMetadata.kind == "poll"`` 的采集 Job，避免误操作
  未来可能出现的调度器内部系统 Job——Job ID 只负责标识，分类只看
  ``metadata.kind``，本用例不解析 ``job_id`` 字符串。

职责边界：本用例只管 Job 粒度操作。Runtime 整体 ``start()``/``stop()``
（Runtime 生命周期）与进程启停（process 生命周期）是另外两层语义，不在
此暴露——三者不得混用。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from wind_hub.application.port.scheduling import (
    POLL_JOB_KIND,
    JobInfo,
    JobState,
    SchedulerPort,
)


class JobDetail(BaseModel):
    """调度 Job 的展示级快照——供 CLI / Web API 呈现。

    在端口层 :class:`~wind_hub.application.port.scheduling.JobInfo` 之上
    补全采集语义：``device_id`` / ``group`` 取自 ``JobMetadata``（非采集
    Job 为 ``None``），``state`` 是二态生命周期视图。
    """

    job_id: str
    """Job 唯一标识（采集 Job 为 ``poll:{device_id}:{group}``）。"""

    device_id: str | None = None
    """采集设备 ID；非 ``poll:`` 前缀的系统 Job 为 ``None``。"""

    group: str | None = None
    """采集分组；非采集 Job 为 ``None``。"""

    interval_seconds: float | None = None
    """触发间隔（秒）。"""

    state: JobState
    """二态生命周期：``RUNNING`` / ``STOPPED``。"""

    next_run_time: datetime | None = None
    """下一次计划触发时间；``STOPPED`` 时为 ``None``。"""


class JobBatchResult(BaseModel):
    """批量 Job 操作（start-all / stop-all）的结果汇总。"""

    total: int
    """参与本次批量操作的采集 Job 总数。"""

    changed: int
    """本次状态发生翻转的 Job 数。"""

    unchanged: int
    """已处于目标状态、本次未触碰的 Job 数。"""


class JobUseCase:
    """采集 Job 管理用例——调度状态操作直接委托给 SchedulerPort。

    未知 ``job_id`` 的 ``KeyError`` 由本用例统一抛出，调用方（适配层）
    据此映射为 404 / 非零退出。
    """

    def __init__(self, scheduler: SchedulerPort) -> None:
        self._scheduler = scheduler

    async def list_jobs(self) -> list[JobDetail]:
        """返回当前全部调度 Job 的展示级快照。"""
        return [self._to_detail(info) for info in self._scheduler.list_jobs()]

    async def get_job(self, job_id: str) -> JobDetail:
        """返回单个 Job 的展示级快照。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        info = self._scheduler.get_job(job_id)
        if info is None:
            raise KeyError(job_id)
        return self._to_detail(info)

    async def start_job(self, job_id: str) -> JobDetail:
        """启动单个 Job 的周期调度（resume 语义）。

        幂等：已 ``RUNNING`` 的 Job 不触碰；不立即执行额外采集——恢复后
        从恢复时刻起按 interval 正常调度。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        info = self._get_or_raise(job_id)
        if info.paused:
            self._scheduler.resume_job(job_id)
        return await self.get_job(job_id)

    async def stop_job(self, job_id: str) -> JobDetail:
        """停止单个 Job 的周期调度（pause 语义）。

        幂等：已 ``STOPPED`` 的 Job 不触碰。不删除 Job、不清理采集执行
        状态、不关闭设备连接，也不影响同设备的其他 group。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        info = self._get_or_raise(job_id)
        if not info.paused:
            self._scheduler.pause_job(job_id)
        return await self.get_job(job_id)

    async def start_all_jobs(self) -> JobBatchResult:
        """启动全部采集 Job（``metadata.kind == "poll"``）——已 RUNNING 的
        保持不变。"""
        return self._set_all(paused=False)

    async def stop_all_jobs(self) -> JobBatchResult:
        """停止全部采集 Job（``metadata.kind == "poll"``）——只暂停采集调度。

        不停止 Scheduler / Runtime，不断开设备连接，不关闭 Sink。
        """
        return self._set_all(paused=True)

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _get_or_raise(self, job_id: str) -> JobInfo:
        """取 Job 端口快照；不存在时抛 ``KeyError``（404 语义）。"""
        info = self._scheduler.get_job(job_id)
        if info is None:
            raise KeyError(job_id)
        return info

    def _set_all(self, *, paused: bool) -> JobBatchResult:
        """把全部采集 Job 置为目标状态；已在目标状态的不计入 changed。"""
        infos = [
            info for info in self._scheduler.list_jobs() if info.metadata.kind == POLL_JOB_KIND
        ]
        changed = 0
        for info in infos:
            if info.paused == paused:
                continue
            if paused:
                self._scheduler.pause_job(info.job_id)
            else:
                self._scheduler.resume_job(info.job_id)
            changed += 1
        return JobBatchResult(total=len(infos), changed=changed, unchanged=len(infos) - changed)

    @staticmethod
    def _to_detail(info: JobInfo) -> JobDetail:
        """把端口层快照补全为展示级模型（device/group 取自元数据）。"""
        return JobDetail(
            job_id=info.job_id,
            device_id=info.metadata.device_id,
            group=info.metadata.group,
            interval_seconds=info.interval_seconds,
            state=info.state,
            next_run_time=info.next_run_time,
        )
