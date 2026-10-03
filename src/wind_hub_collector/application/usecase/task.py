"""Task use case——采集 Task / Task Instance 显式生命周期控制的应用编排。

基于 :class:`~wind_hub_collector.application.runtime.runtime.Runtime`，供 Collector gRPC
控制面管理采集任务：查询 Task 定义与展开后的实例，以及显式 start/stop
Task 或单个 Task Instance。

核心语义：

- 配置决定「有哪些 Task 与实例」——定义与实例的创建/删除只发生在启动
  装配与配置热重载；本用例的 start/stop 只翻转实例生命周期状态
  （RUNNING / STOPPED），不增删实例；
- 实例状态只有两态——采集执行状态（failed/partial）与设备连接状态
  （disconnected）是另外的维度，不由本用例呈现或修改。

职责边界：本用例只管 Task / Task Instance 粒度操作。Runtime 整体
``start()``/``stop()``（Runtime 生命周期）与进程启停（process 生命周期）
是另外两层语义，不在此暴露——三者不得混用。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub_collector.application.runtime.runtime import Runtime
from wind_hub_collector.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
)
from wind_hub_core.config.schema import CollectionTaskConfig


class TaskInstanceDetail(BaseModel):
    """Task Instance 的展示级快照——实例定义 + 生命周期状态。"""

    instance_id: str
    """实例标识（``{task_id}:{device_id}``）。"""

    task_id: str
    device_id: str
    point_group: str
    interval: float | None
    targets: list[str]
    state: TaskInstanceState
    """二态生命周期：``RUNNING`` / ``STOPPED``。"""


class TaskSummary(BaseModel):
    """Task Definition 与实例运行状态的聚合快照。"""

    task_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None
    targets: list[str]
    enabled: bool
    runtime_state: str
    """聚合生命周期状态：running / stopped / partial。"""
    instance_count: int
    running_instances: int
    stopped_instances: int
    failed_instances: int
    """最近采集连续失败大于 0 的实例数；不改变生命周期状态。"""


class TaskUseCase:
    """采集 Task 管理用例——实例启停直接委托给 Runtime。

    未知 ``instance_id`` 的 ``KeyError`` 由本用例统一抛出，调用方
    （适配层）据此映射为 404 / 非零退出。
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def list_instances(self) -> list[TaskInstanceDetail]:
        """返回当前全部 Task Instance 的展示级快照。

        Returns:
            TaskInstanceDetail 列表。
        """
        states = self._runtime.instance_states()
        return [
            self._to_detail(inst, states[inst.instance_id])
            for inst in self._runtime.task_instances().values()
        ]

    async def get_instance(self, instance_id: str) -> TaskInstanceDetail:
        """返回单个实例的展示级快照。

        Args:
            instance_id: Task Instance 稳定标识。

        Returns:
            当前实例定义与生命周期状态。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        inst = self._get_or_raise(instance_id)
        return self._to_detail(inst, self._runtime.instance_states()[instance_id])

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        """启动单个实例的持续采集。

        Args:
            instance_id: Task Instance 稳定标识。

        Returns:
            启动后的实例快照。

        Raises:
            KeyError: ``instance_id`` 不存在。

        Notes:
            操作幂等；POLL 协议第一轮按 fixed-rate 节拍立即开始，订阅协议注册后
            等待远端推送。
        """
        await self._runtime.start_task_instance(instance_id)
        return await self.get_instance(instance_id)

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        """停止单个实例的持续采集。

        Args:
            instance_id: Task Instance 稳定标识。

        Returns:
            停止后的实例快照。

        Raises:
            KeyError: ``instance_id`` 不存在。

        Notes:
            操作幂等；不删除实例、不断开设备连接。
        """
        await self._runtime.stop_task_instance(instance_id)
        return await self.get_instance(instance_id)

    async def list_task_summaries(self) -> list[TaskSummary]:
        """返回全部 Task 的定义与实例聚合状态。

        Returns:
            TaskSummary 列表。
        """
        return [
            await self.get_task_summary(task_id)
            for task_id in self._runtime.task_definitions()
        ]

    async def get_task_summary(self, task_id: str) -> TaskSummary:
        """返回单个 Task 的聚合状态。

        Args:
            task_id: Task Definition 稳定标识。

        Returns:
            Task 定义、实例数量和聚合生命周期状态。

        Raises:
            KeyError: task_id 不存在。
        """
        task = self._task_definition_or_raise(task_id)
        instances = [
            inst
            for inst in self._runtime.task_instances().values()
            if inst.task_id == task_id
        ]
        states = self._runtime.instance_states()
        acquisition = self._runtime.acquisition_states()
        running = sum(
            1 for inst in instances if states[inst.instance_id] is TaskInstanceState.RUNNING
        )
        stopped = len(instances) - running
        failed = sum(
            1
            for inst in instances
            if acquisition.get(inst.instance_id) is not None
            and acquisition[inst.instance_id].consecutive_failures > 0
        )
        runtime_state = "stopped" if running == 0 else "running" if stopped == 0 else "partial"
        return TaskSummary(
            task_id=task.task_id,
            device=task.device,
            device_group=task.device_group,
            point_group=task.point_group,
            interval=task.interval,
            targets=[target.sink for target in task.targets],
            enabled=task.enabled,
            runtime_state=runtime_state,
            instance_count=len(instances),
            running_instances=running,
            stopped_instances=stopped,
            failed_instances=failed,
        )

    async def list_task_instances(self, task_id: str) -> list[TaskInstanceDetail]:
        """返回指定 Task 展开的全部实例。

        Args:
            task_id: Task Definition 稳定标识。

        Returns:
            属于该 Task 的 TaskInstanceDetail 列表。

        Raises:
            KeyError: task_id 不存在。
        """
        self._task_definition_or_raise(task_id)
        states = self._runtime.instance_states()
        return [
            self._to_detail(inst, states[inst.instance_id])
            for inst in self._runtime.task_instances().values()
            if inst.task_id == task_id
        ]

    async def start_task(self, task_id: str) -> TaskSummary:
        """启动一个 Task 展开的全部实例，并返回聚合状态。

        Args:
            task_id: Task Definition 稳定标识。

        Returns:
            启动后的 TaskSummary。

        Raises:
            KeyError: task_id 不存在。
            ValueError: Task 被禁用，不能启动。
        """
        task = self._task_definition_or_raise(task_id)
        if not task.enabled:
            raise ValueError(f"task '{task_id}' is disabled")
        for inst in await self.list_task_instances(task_id):
            await self._runtime.start_task_instance(inst.instance_id)
        return await self.get_task_summary(task_id)

    async def stop_task(self, task_id: str) -> TaskSummary:
        """停止一个 Task 展开的全部实例，并返回聚合状态。

        Args:
            task_id: Task Definition 稳定标识。

        Returns:
            停止后的 TaskSummary。

        Raises:
            KeyError: task_id 不存在。
        """
        self._task_definition_or_raise(task_id)
        for inst in await self.list_task_instances(task_id):
            await self._runtime.stop_task_instance(inst.instance_id)
        return await self.get_task_summary(task_id)

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _task_definition_or_raise(self, task_id: str) -> CollectionTaskConfig:
        """取 Task Definition；不存在时抛 KeyError。"""
        task = self._runtime.task_definitions().get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _get_or_raise(self, instance_id: str) -> CollectionTaskInstance:
        """取实例快照；不存在时抛 ``KeyError``（404 语义）。"""
        inst = self._runtime.task_instances().get(instance_id)
        if inst is None:
            raise KeyError(instance_id)
        return inst

    @staticmethod
    def _to_detail(inst: CollectionTaskInstance, state: TaskInstanceState) -> TaskInstanceDetail:
        return TaskInstanceDetail(
            instance_id=inst.instance_id,
            task_id=inst.task_id,
            device_id=inst.device_id,
            point_group=inst.point_group,
            interval=inst.interval,
            targets=list(inst.targets),
            state=state,
        )
