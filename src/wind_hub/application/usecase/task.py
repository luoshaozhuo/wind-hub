"""Task use case——采集 Task / Task Instance 显式生命周期控制的应用编排。

基于 :class:`~wind_hub.application.runtime.runtime.Runtime`，供 CLI /
Web API 管理采集任务：查询 Task 定义与展开后的实例、start/stop 单个实例、
批量启停。

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

from wind_hub.application.runtime.runtime import Runtime
from wind_hub.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
)


class TaskDetail(BaseModel):
    """Task Definition 的展示级快照——供 CLI / Web API 呈现。"""

    task_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float
    targets: list[str]
    enabled: bool


class TaskInstanceDetail(BaseModel):
    """Task Instance 的展示级快照——实例定义 + 生命周期状态。"""

    instance_id: str
    """实例标识（``{task_id}:{device_id}``）。"""

    task_id: str
    device_id: str
    point_group: str
    interval: float
    targets: list[str]
    state: TaskInstanceState
    """二态生命周期：``RUNNING`` / ``STOPPED``。"""


class TaskBatchResult(BaseModel):
    """批量实例操作（start-all / stop-all）的结果汇总。"""

    total: int
    """参与本次批量操作的实例总数。"""

    changed: int
    """本次状态发生翻转的实例数。"""

    unchanged: int
    """已处于目标状态、本次未触碰的实例数。"""


class TaskUseCase:
    """采集 Task 管理用例——实例启停直接委托给 Runtime。

    未知 ``instance_id`` 的 ``KeyError`` 由本用例统一抛出，调用方
    （适配层）据此映射为 404 / 非零退出。
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def list_tasks(self) -> list[TaskDetail]:
        """返回当前全部 Task Definition 的展示级快照。"""
        return [
            TaskDetail(
                task_id=t.task_id,
                device=t.device,
                device_group=t.device_group,
                point_group=t.point_group,
                interval=t.interval,
                targets=[target.sink for target in t.targets],
                enabled=t.enabled,
            )
            for t in self._runtime.task_definitions().values()
        ]

    async def list_instances(self) -> list[TaskInstanceDetail]:
        """返回当前全部 Task Instance 的展示级快照。"""
        states = self._runtime.instance_states()
        return [
            self._to_detail(inst, states[inst.instance_id])
            for inst in self._runtime.task_instances().values()
        ]

    async def get_instance(self, instance_id: str) -> TaskInstanceDetail:
        """返回单个实例的展示级快照。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        inst = self._get_or_raise(instance_id)
        return self._to_detail(inst, self._runtime.instance_states()[instance_id])

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        """启动单个实例的周期采集（幂等；不立即执行额外采集——协程第一
        轮 collect 在创建后随即开始，之后按 interval 循环）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        await self._runtime.start_task_instance(instance_id)
        return await self.get_instance(instance_id)

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        """停止单个实例的周期采集（幂等；不删除实例、不断开设备连接）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        await self._runtime.stop_task_instance(instance_id)
        return await self.get_instance(instance_id)

    async def start_all_instances(self) -> TaskBatchResult:
        """启动全部 Task Instance——已 RUNNING 的保持不变。"""
        return await self._set_all(start=True)

    async def stop_all_instances(self) -> TaskBatchResult:
        """停止全部 Task Instance 的周期采集。

        不停止 Runtime，不断开设备连接，不关闭 Sink。
        """
        return await self._set_all(start=False)

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _get_or_raise(self, instance_id: str) -> CollectionTaskInstance:
        """取实例快照；不存在时抛 ``KeyError``（404 语义）。"""
        inst = self._runtime.task_instances().get(instance_id)
        if inst is None:
            raise KeyError(instance_id)
        return inst

    async def _set_all(self, *, start: bool) -> TaskBatchResult:
        """把全部实例置为目标状态；已在目标状态的不计入 changed。"""
        states = self._runtime.instance_states()
        target = TaskInstanceState.RUNNING if start else TaskInstanceState.STOPPED
        changed = 0
        for instance_id, state in states.items():
            if state is target:
                continue
            if start:
                await self._runtime.start_task_instance(instance_id)
            else:
                await self._runtime.stop_task_instance(instance_id)
            changed += 1
        return TaskBatchResult(total=len(states), changed=changed, unchanged=len(states) - changed)

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
