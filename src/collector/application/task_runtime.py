"""Collector 采集 Task 与持续采集生命周期的唯一权威。

只管理 Task 子系统：Task Definition 按设备展开为 Task Instance，实例启停、
采集句柄、采集执行状态与 restart_pending 全部由本对象簿记。设备会话来自
DeviceRuntime（依赖方向 TaskRuntime → DeviceRuntime，无反向依赖）；一次
collect 的业务执行仍属于 Domain AcquisitionEngine，本对象只决定「哪个实例
在持续采集」并记录其状态；Sink 派发不经由本对象。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..domain.acquisition import AcquisitionEngine
from ..domain.point_value import PointValue
from .acquisition_state import AcquisitionRuntimeState
from .config import CollectionTask
from .device_runtime import DeviceRuntime
from .session import AcquisitionHandle
from .task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
    task_instance_id,
)

if TYPE_CHECKING:
    from .runtime import RuntimeMetricsPort

logger = logging.getLogger(__name__)


class TaskRuntime:
    """持有 Task 注册表与采集生命周期状态，并负责实例展开与启停。

    六类簿记（Task Definition / Task Instance / TaskInstanceState /
    AcquisitionHandle / AcquisitionRuntimeState / restart_pending）的唯一
    权威；不保存设备会话（DeviceRuntime）或 Sink 状态（SinkRuntime）。
    三种状态维度严格分离：设备连接状态在 DeviceRuntime，实例生命周期状态
    （RUNNING/STOPPED）与采集执行状态（AcquisitionRuntimeState）在本对象
    内也分别簿记，互不反推。
    """

    def __init__(
        self,
        task_defs: dict[str, CollectionTask],
        devices: DeviceRuntime,
        engine: AcquisitionEngine,
        clock: Callable[[], float] = time.monotonic,
        metrics_hook: RuntimeMetricsPort | None = None,
    ) -> None:
        self._task_defs: dict[str, CollectionTask] = dict(task_defs)
        self._devices = devices
        self._engine = engine
        self._clock = clock
        self._metrics = metrics_hook

        # 采集 Task 运行时三簿记：
        # - ``_task_instances``：展开后的 Task Instance 快照（不可变，热重载
        #   整体替换）；
        # - ``_instance_states``：实例生命周期状态（RUNNING/STOPPED，显式
        #   簿记——acquisition handle 是否存在由它决定，不反向推断）；
        # - ``_acquisition_handles``：运行中实例的采集句柄（polling 协程
        #   或协议订阅，TaskRuntime 不感知机制），不留 orphan 资源。
        self._task_instances: dict[str, CollectionTaskInstance] = {}
        self._instance_states: dict[str, TaskInstanceState] = {}
        self._acquisition_handles: dict[str, AcquisitionHandle] = {}
        # 热重载过程中需要恢复 RUNNING、但上一次重建失败的实例。
        # 下一次 reload 会继续重试，直到成功或实例被显式停止/删除。
        self._restart_pending: set[str] = set()

        # 每个采集实例的业务执行状态——与设备连接状态、实例启停状态分维度。
        # 实例注册时建立、注销时删除；引擎 collect 经 AcquisitionStatePort
        # 上报演进。
        self._acquisition_states: dict[str, AcquisitionRuntimeState] = {}

    # ------------------------------------------------------------------
    # 只读视图（查询/控制服务经此读取，热重载安全）
    # ------------------------------------------------------------------

    def attach_metrics_hook(self, metrics_hook: RuntimeMetricsPort | None) -> None:
        """与 Collector 的指标 observer 同步替换，不改变实例启停状态。"""
        self._metrics = metrics_hook

    def task_definitions(self) -> dict[str, CollectionTask]:
        """当前 Task Definition 注册表（浅拷贝）。"""
        return dict(self._task_defs)

    def task_instances(self) -> dict[str, CollectionTaskInstance]:
        """当前展开后的 Task Instance 注册表（浅拷贝）。"""
        return dict(self._task_instances)

    def instance_states(self) -> dict[str, TaskInstanceState]:
        """各 Task Instance 的生命周期状态（浅拷贝）。"""
        return dict(self._instance_states)

    def acquisition_states(self) -> dict[str, AcquisitionRuntimeState]:
        """当前采集实例执行状态簿（``{instance_id: state}`` 浅拷贝）。"""
        return dict(self._acquisition_states)

    # ------------------------------------------------------------------
    # 实例生命周期（控制面的操作面）
    # ------------------------------------------------------------------

    async def start_instance(self, instance_id: str) -> None:
        """启动单个 Task Instance 的持续采集（经会话获取采集句柄）。

        幂等：已 RUNNING 的实例不触碰——同一实例绝不会出现两份 handle。
        采集机制（fixed-rate polling / 协议订阅）由会话按协议能力与进程
        级订阅策略决定，本方法不感知。

        Raises:
            KeyError: ``instance_id`` 不存在。
            Exception: 采集启动失败（如订阅注册失败）——状态保持 STOPPED，
                错误原样上抛给控制面调用方。
        """
        instance = self._task_instances.get(instance_id)
        if instance is None:
            raise KeyError(instance_id)
        if self._instance_states[instance_id] is TaskInstanceState.RUNNING:
            return
        device = self._devices.devices.get(instance.device_id)
        if device is None:
            raise KeyError(f"device '{instance.device_id}' for instance '{instance_id}'")

        async def acquire() -> None:
            # POLL 型句柄的每 tick 执行体——实例快照现取，热重载替换
            # targets 无需重启 handle。
            current = self._task_instances.get(instance_id)
            if current is None:
                return
            await self._engine.collect(
                device,
                current.point_group,
                list(current.targets),
                instance_id,
            )

        async def on_data(values: list[PointValue]) -> None:
            # 订阅推送的数据落点——数据已到达，直接进入统一处理入口，
            # 不绕回主动 collect。
            current = self._task_instances.get(instance_id)
            if current is None:
                return
            await self._engine.process(values, list(current.targets))

        handle = await device.start_acquisition(
            point_group=instance.point_group,
            interval=instance.interval,
            acquire=acquire,
            on_data=on_data,
            on_poll_stats=self._poll_stats_hook(instance),
        )
        self._acquisition_handles[instance_id] = handle
        self._instance_states[instance_id] = TaskInstanceState.RUNNING
        self._restart_pending.discard(instance_id)

    async def stop_instance(self, instance_id: str) -> None:
        """停止单个 Task Instance 的持续采集（关闭采集句柄）。

        幂等：已 STOPPED 的实例不触碰。不删除实例、不清理采集执行状态、
        不关闭设备连接，也不影响其他实例（订阅型句柄只注销本实例的订阅）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        if instance_id not in self._task_instances:
            raise KeyError(instance_id)
        if self._instance_states[instance_id] is TaskInstanceState.STOPPED:
            self._restart_pending.discard(instance_id)
            return
        self._instance_states[instance_id] = TaskInstanceState.STOPPED
        self._restart_pending.discard(instance_id)
        await self._close_acquisition_handle(instance_id)

    async def stop_all(self) -> None:
        """停机路径：关闭全部实例采集句柄——polling 协程取消、订阅注销，
        不留后台 task 或订阅。

        实例定义保留，统一标记 STOPPED——重启后需显式 start，与启动语义
        一致。句柄关闭失败只记录不阻断。
        """
        for instance_id in list(self._acquisition_handles):
            await self._close_acquisition_handle(instance_id)
        for instance_id in self._instance_states:
            self._instance_states[instance_id] = TaskInstanceState.STOPPED

    # ------------------------------------------------------------------
    # 设备变更协调（CollectorRuntime 在 session 变更前后调用）
    # ------------------------------------------------------------------

    async def suspend_for_device_change(self, device_id: str) -> list[str]:
        """设备 session 变更前：停止该设备全部 RUNNING 实例并关闭其句柄。

        Returns:
            原本 RUNNING 的实例 ID 列表，供变更完成后恢复其运行状态。
        """
        was_running = [
            iid
            for iid, inst in self._task_instances.items()
            if inst.device_id == device_id
            and self._instance_states.get(iid) is TaskInstanceState.RUNNING
        ]
        for iid in was_running:
            self._instance_states[iid] = TaskInstanceState.STOPPED
            await self._close_acquisition_handle(iid)
        return was_running

    async def resume_after_device_change(self, instance_ids: Iterable[str]) -> None:
        """设备 session 变更后：恢复仍在展开结果中的原 RUNNING 实例。

        恢复失败只记录日志——实例保持 STOPPED，由后续 reload 或显式控制
        再次尝试（与原 rebuild 恢复语义一致，不进入 restart_pending）。
        """
        for iid in instance_ids:
            if iid not in self._task_instances:
                continue
            try:
                await self.start_instance(iid)
            except Exception:
                logger.warning(
                    "Hot-reload: task instance '%s' failed to restart after device rebuild",
                    iid,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # 实例同步（启动、热重载与设备增删重建后的统一收尾）
    # ------------------------------------------------------------------

    async def apply_definitions(
        self,
        task_defs: dict[str, CollectionTask],
        *,
        restart_subscription_devices: set[str] | None = None,
    ) -> None:
        """整体替换 Task Definition 注册表并把实例簿同步到最新展开结果。"""
        self._task_defs = dict(task_defs)
        await self.sync_instances(restart_subscription_devices=restart_subscription_devices)

    async def sync_instances(
        self,
        *,
        restart_subscription_devices: set[str] | None = None,
    ) -> None:
        """把 Task Instance 注册表同步为「当前 Task 定义 × 当前设备」的展开
        结果。

        - 消失的实例：关闭采集句柄、删除实例与其生命周期/执行状态；
        - 新增的实例：以 STOPPED 注册（与启动语义一致——显式 start 才运行）；
        - 仍存在的实例：整体替换为最新快照；运行中实例的 interval /
          point_group 变化会重建采集句柄（poll 重新对齐节拍、订阅重新
          注册），targets 单独变化无需重建（回调每轮现取最新实例）。

        本方法幂等，设备增删/重建/轻量更新与 Task diff 应用后都会调用。
        """
        desired = self._desired_instances()
        restart_subscription_devices = restart_subscription_devices or set()

        for iid in set(self._task_instances) - set(desired):
            await self._close_acquisition_handle(iid)
            self._task_instances.pop(iid, None)
            self._instance_states.pop(iid, None)
            self._acquisition_states.pop(iid, None)
            self._restart_pending.discard(iid)
            logger.info("Task instance '%s' unregistered", iid)

        for iid, instance in desired.items():
            old = self._task_instances.get(iid)
            self._task_instances[iid] = instance
            if old is None:
                self._instance_states[iid] = TaskInstanceState.STOPPED
                logger.info("Task instance '%s' registered (stopped)", iid)
            else:
                was_running = self._instance_states[iid] is TaskInstanceState.RUNNING
                needs_restart = iid in self._restart_pending or (
                    was_running
                    and (
                        old.interval != instance.interval
                        or old.point_group != instance.point_group
                        or instance.device_id in restart_subscription_devices
                    )
                )
                if needs_restart:
                    # 节拍、选点或订阅地址变化：重建采集句柄并恢复 RUNNING。
                    # 失败时保留 pending，reconfigure 向上返回错误，下一次 reload
                    # 继续重试，避免“reload 成功但实例永久 STOPPED”。
                    self._restart_pending.add(iid)
                    if was_running:
                        self._instance_states[iid] = TaskInstanceState.STOPPED
                        await self._close_acquisition_handle(iid)
                    try:
                        await self.start_instance(iid)
                    except Exception:
                        logger.warning(
                            "Task instance '%s' failed to restart acquisition after reload",
                            iid,
                            exc_info=True,
                        )
                        raise
                    else:
                        self._restart_pending.discard(iid)
            state = self._acquisition_states.get(iid)
            if (
                state is None
                or state.task_id != instance.task_id
                or state.point_group != instance.point_group
            ):
                self._acquisition_states[iid] = AcquisitionRuntimeState(
                    instance_id=iid,
                    task_id=instance.task_id,
                    device_id=instance.device_id,
                    point_group=instance.point_group,
                )

    def _desired_instances(self) -> dict[str, CollectionTaskInstance]:
        """展开当前 Task 定义为 Task Instance 集。

        - ``enabled: false`` 的 Task 不展开（不创建运行实例）；
        - ``device`` Task 只在设备存在时展开（停用设备不进快照，天然排除）；
        - ``device_group`` Task 对每台 ``device_group`` 匹配的设备展开一个
          实例。
        """
        desired: dict[str, CollectionTaskInstance] = {}
        for task in self._task_defs.values():
            if not task.enabled:
                continue
            if task.device is not None:
                device_ids = [task.device] if task.device in self._devices.devices else []
            else:
                device_ids = [
                    device_id
                    for device_id, session in self._devices.devices.items()
                    if task.device_group in session.device_group_ids
                ]
            for device_id in device_ids:
                iid = task_instance_id(task.task_id, device_id)
                desired[iid] = CollectionTaskInstance(
                    instance_id=iid,
                    task_id=task.task_id,
                    device_id=device_id,
                    point_group=task.point_group,
                    interval=task.interval,
                    targets=task.targets,
                )
        return desired

    # ------------------------------------------------------------------
    # 采集执行状态端口实现（AcquisitionEngine → TaskRuntime 的 collect 钩子）
    # ------------------------------------------------------------------

    def report_collect_started(self, execution_id: str, device_id: str, group: str) -> None:
        """一次 collect 开始（实现 ``AcquisitionStatePort``）。"""
        self._acq_state_for(execution_id, device_id, group).begin(self._clock())

    def report_collect_success(
        self, execution_id: str, device_id: str, group: str, *, partial: bool
    ) -> None:
        """一次 collect 成功（含 partial——GOOD/BAD 混合不计连续失败）。"""
        state = self._acq_state_for(execution_id, device_id, group)
        state.finish_success(self._clock())
        if self._metrics is not None:
            self._metrics.acquisition_run_finished(
                device_id, group, "partial" if partial else "success", state.last_duration
            )

    def report_collect_failure(
        self, execution_id: str, device_id: str, group: str, error: str
    ) -> None:
        """一次 collect 失败（读异常/读超时/断线跳过/无有效结果）。"""
        state = self._acq_state_for(execution_id, device_id, group)
        state.finish_failure(self._clock(), error)
        if self._metrics is not None:
            self._metrics.acquisition_run_finished(device_id, group, "failed", state.last_duration)

    def _acq_state_for(
        self, execution_id: str, device_id: str, group: str
    ) -> AcquisitionRuntimeState:
        """取采集执行状态；缺失时按当前实例信息创建（引擎只对运行中实例
        上报，实例必然已注册）。"""
        state = self._acquisition_states.get(execution_id)
        if state is None:
            inst = self._task_instances[execution_id]
            state = AcquisitionRuntimeState(
                instance_id=execution_id,
                task_id=inst.task_id,
                device_id=device_id,
                point_group=group,
            )
            self._acquisition_states[execution_id] = state
        return state

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _poll_stats_hook(
        self, instance: CollectionTaskInstance
    ) -> Callable[[float, bool, int], None] | None:
        """为 POLL 型采集构造指标回调；未注入指标端口时返回 ``None``。"""
        if self._metrics is None:
            return None
        metrics = self._metrics
        device_id = instance.device_id
        group = instance.point_group

        def _report(jitter: float, overrun: bool, missed: int) -> None:
            metrics.acquisition_poll_stats(device_id, group, jitter, overrun, missed)

        return _report

    async def _close_acquisition_handle(self, instance_id: str) -> None:
        """关闭并摘除实例的采集句柄（幂等）；关闭失败只记录不阻断。"""
        handle = self._acquisition_handles.pop(instance_id, None)
        if handle is None:
            return
        try:
            await handle.close()
        except Exception:
            logger.warning(
                "Task instance '%s' acquisition handle close failed",
                instance_id,
                exc_info=True,
            )
