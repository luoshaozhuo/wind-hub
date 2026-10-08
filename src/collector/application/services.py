"""Collector 查询/控制应用服务——gRPC 控制面委托的用例编排。

基于 :class:`CollectorRuntime` 的只读快照与实例生命周期委托：

- Query：设备注册表/连接状态与 Runtime 聚合状态；
- Task：Task Definition 与展开实例的查询、显式 start/stop——只翻转实例
  生命周期状态（RUNNING / STOPPED），不增删实例（实例的创建/删除只发生
  在启动装配与配置热重载）；
- Sink：运行 Sink 健康/队列深度查询与显式诊断写——不持有任何 Sink 状态。

所有读取都穿透 CollectorRuntime 当前状态，热重载后立即可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.point_value import PointValue
from .config import CollectionTask
from .runtime import CollectorRuntime
from .task_instance import CollectionTaskInstance, TaskInstanceState


@dataclass(frozen=True, slots=True)
class AcquisitionInfo:
    """单个采集实例（Task Instance）的业务执行状态快照。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    running: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_duration: float | None = None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """单个设备的连接状态快照（``last_seen`` 暂无墙钟追踪，恒为 None）。"""

    device_id: str
    protocol: str
    connected: bool
    consecutive_failures: int = 0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class SystemStatus:
    """CollectorRuntime 聚合状态快照。"""

    running: bool
    device_count: int
    sink_count: int
    devices_connected: int = 0
    sinks_healthy: int = 0
    points_collected: int = 0
    points_routed: int = 0
    points_dropped: int = 0
    acquisitions: list[AcquisitionInfo] = field(default_factory=list)


class CollectorQueryService:
    """Collector 运行事实查询服务——所有读取都穿透 CollectorRuntime 当前状态。"""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

    async def list_devices(self) -> list[DeviceInfo]:
        """返回当前注册表中全部设备运行状态。"""
        return [self._device_info(device_id) for device_id in self._runtime.devices]

    async def status(self) -> SystemStatus:
        """返回 CollectorRuntime 聚合状态快照。"""
        devices_connected = sum(1 for h in self._runtime.device_health().values() if h.healthy)
        sinks_healthy = sum(1 for h in self._runtime.sink_health().values() if h.healthy)
        return SystemStatus(
            running=self._runtime.running,
            device_count=self._runtime.device_count,
            sink_count=self._runtime.sink_count,
            devices_connected=devices_connected,
            sinks_healthy=sinks_healthy,
            points_collected=self._runtime.points_collected,
            points_routed=self._runtime.points_routed,
            points_dropped=self._runtime.points_dropped,
            acquisitions=[
                AcquisitionInfo(
                    instance_id=state.instance_id,
                    task_id=state.task_id,
                    device_id=state.device_id,
                    point_group=state.point_group,
                    running=state.running,
                    consecutive_failures=state.consecutive_failures,
                    last_error=state.last_error,
                    last_duration=state.last_duration,
                )
                for state in self._runtime.acquisition_states().values()
            ],
        )

    def _device_info(self, device_id: str) -> DeviceInfo:
        """从协议健康状态 + DeviceRuntime 设备运行状态构造 DeviceInfo。

        connected 以驱动实时 health 为准；consecutive_failures/last_error
        来自 DeviceRuntime 的重连节流状态。
        """
        session = self._runtime.devices.get(device_id)
        state = self._runtime.device_state(device_id)
        connected = session.health().healthy if session is not None else False
        protocol = session.protocol_name if session is not None else ""
        return DeviceInfo(
            device_id=device_id,
            protocol=protocol,
            connected=connected,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            last_error=state.last_error if state is not None else None,
        )


@dataclass(frozen=True, slots=True)
class TaskInstanceDetail:
    """Task Instance 的展示级快照——实例定义 + 生命周期状态。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None
    targets: list[str]
    state: TaskInstanceState
    """二态生命周期：``RUNNING`` / ``STOPPED``。"""


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """Task Definition 与实例运行状态的聚合快照。"""

    task_id: str
    device: str | None
    device_group: str | None
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


class CollectorTaskService:
    """采集 Task 管理服务——实例启停直接委托给 CollectorRuntime。

    未知 ``instance_id`` / ``task_id`` 的 ``KeyError`` 由本服务统一抛出，
    调用方（适配层）据此映射为 NOT_FOUND。
    """

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

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
        inst = self._instance_or_raise(instance_id)
        return self._to_detail(inst, self._runtime.instance_states()[instance_id])

    async def start_instance(self, instance_id: str) -> TaskInstanceDetail:
        """启动单个实例的持续采集（幂等）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        await self._runtime.start_task_instance(instance_id)
        return await self.get_instance(instance_id)

    async def stop_instance(self, instance_id: str) -> TaskInstanceDetail:
        """停止单个实例的持续采集（幂等；不删除实例、不断开设备连接）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        await self._runtime.stop_task_instance(instance_id)
        return await self.get_instance(instance_id)

    async def list_task_summaries(self) -> list[TaskSummary]:
        """返回全部 Task 的定义与实例聚合状态。"""
        return [
            await self.get_task_summary(task_id) for task_id in self._runtime.task_definitions()
        ]

    async def get_task_summary(self, task_id: str) -> TaskSummary:
        """返回单个 Task 的聚合状态。

        Raises:
            KeyError: task_id 不存在。
        """
        task = self._task_definition_or_raise(task_id)
        instances = [
            inst for inst in self._runtime.task_instances().values() if inst.task_id == task_id
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
            targets=list(task.targets),
            enabled=task.enabled,
            runtime_state=runtime_state,
            instance_count=len(instances),
            running_instances=running,
            stopped_instances=stopped,
            failed_instances=failed,
        )

    async def list_task_instances(self, task_id: str) -> list[TaskInstanceDetail]:
        """返回指定 Task 展开的全部实例。

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

        Raises:
            KeyError: task_id 不存在。
        """
        self._task_definition_or_raise(task_id)
        for inst in await self.list_task_instances(task_id):
            await self._runtime.stop_task_instance(inst.instance_id)
        return await self.get_task_summary(task_id)

    def _task_definition_or_raise(self, task_id: str) -> CollectionTask:
        """取 Task Definition；不存在时抛 KeyError。"""
        task = self._runtime.task_definitions().get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _instance_or_raise(self, instance_id: str) -> CollectionTaskInstance:
        """取实例快照；不存在时抛 ``KeyError``（404 语义）。"""
        inst = self._runtime.task_instances().get(instance_id)
        if inst is None:
            raise KeyError(instance_id)
        return inst

    @staticmethod
    def _to_detail(
        inst: CollectionTaskInstance,
        state: TaskInstanceState,
    ) -> TaskInstanceDetail:
        return TaskInstanceDetail(
            instance_id=inst.instance_id,
            task_id=inst.task_id,
            device_id=inst.device_id,
            point_group=inst.point_group,
            interval=inst.interval,
            targets=list(inst.targets),
            state=state,
        )


@dataclass(frozen=True, slots=True)
class SinkInfo:
    """单个运行 Sink 的健康与队列深度快照。"""

    name: str
    healthy: bool = False
    message: str = ""
    queue_depth: int = 0


@dataclass(frozen=True, slots=True)
class SinkWriteResult:
    """诊断测试写结果。"""

    success: bool
    message: str = ""


class CollectorSinkService:
    """Collector 运行 Sink 的检查与诊断写入口。"""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

    async def list_sinks(self) -> list[SinkInfo]:
        """列出当前运行 Sink 的健康状态与队列深度。"""
        health = self._runtime.sink_health()
        depths = self._runtime.sink_queue_depths()
        items: list[SinkInfo] = []
        for name in self._runtime.sinks:
            current = health.get(name)
            items.append(
                SinkInfo(
                    name=name,
                    healthy=bool(current.healthy) if current is not None else False,
                    message=(current.message or "") if current is not None else "",
                    queue_depth=int(depths.get(name, 0)),
                )
            )
        return items

    async def verify_sink(self, name: str) -> SinkInfo:
        """返回指定运行 Sink 的真实 health 与队列深度。

        Raises:
            KeyError: Sink 未注册。
        """
        sink = self._runtime.sinks.get(name)
        if sink is None:
            raise KeyError(name)
        health = sink.health()
        return SinkInfo(
            name=name,
            healthy=bool(health.healthy),
            message=health.message or "",
            queue_depth=int(self._runtime.sink_queue_depths().get(name, 0)),
        )

    async def write_test_sink(self, name: str) -> SinkWriteResult:
        """向指定运行 Sink 写入诊断测试点并 flush。

        Raises:
            KeyError: Sink 未注册。
        """
        sink = self._runtime.sinks.get(name)
        if sink is None:
            raise KeyError(name)
        try:
            await sink.write([PointValue(device_id="_diagnostic", point_id="_write_test", value=1)])
            await sink.flush()
        except Exception as exc:
            return SinkWriteResult(
                success=False,
                message=str(exc) or type(exc).__name__,
            )
        return SinkWriteResult(success=True)
