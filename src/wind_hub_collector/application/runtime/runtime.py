"""Runtime —— 采集系统的运行时组件管理与生命周期编排核心。

架构位置：application 层。职责：

- 设备生命周期（连接 / 关闭 / 重建）——以 :class:`Device` 为唯一聚合；
- Sink 生命周期（打开 / 队列 / 消费者任务 / 背压 / 关闭）；
- 采集 Task：Task Definition（``tasks.yaml``）按设备展开为
  :class:`CollectionTaskInstance`；显式 start 时经
  ``Device.start_acquisition`` 获得 :class:`AcquisitionHandle`（底层是
  fixed-rate polling 或协议订阅，Runtime 不感知），stop 时关闭句柄；
- 设备与 Sink 的增删 / 重建，配置热重载时的运行时重构（:meth:`reconfigure`）；
- Runtime 状态（running / health / 组件计数 / 点位统计）；
- 整体 ``start()`` / ``stop()``。

持有：``dict[str, Device]``（设备的唯一权威——配置、点表、协议实例都
聚合在 ``Device`` 内）、:class:`~wind_hub_collector.domain.acquisition.AcquisitionEngine`
（PointValue 数据流处理）、
:class:`~wind_hub_collector.application.command_dispatcher.CommandDispatcher`（命令分发）。

不负责：协议实现细节（ProtocolPort 适配器）、配置加载与 diff
（ConfigUseCase）、采集时序（acquisition handle）。

失败语义：设备连接与 sink 打开均为 best-effort——单个失败记录日志并跳过，
其余组件照常启动，失败组件经 :meth:`health` 暴露为不健康。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Protocol

from wind_hub_collector.application.command_dispatcher import CommandDispatcher
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime.acquisition_state import AcquisitionRuntimeState
from wind_hub_collector.application.runtime.device import AcquisitionHandle, Device
from wind_hub_collector.application.runtime.device_state import DeviceRuntimeState
from wind_hub_collector.application.runtime.dispatcher import RuntimeSinkDispatcher
from wind_hub_collector.application.runtime.lifecycle import RuntimeLifecycle
from wind_hub_collector.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
    task_instance_id,
)
from wind_hub_collector.domain.acquisition.engine import AcquisitionEngine
from wind_hub_core.config.schema import (
    CollectionTaskConfig,
    Config,
    DeviceConfig,
    PointConfig,
    RuntimeConfig,
    SinkConfig,
)
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue
from wind_hub_core.model.reload import ConfigDiff, DeviceDiff, SinkDiff, TaskDiff
from wind_hub_core.protocol.port import AcquisitionMode, ProtocolPort

logger = logging.getLogger(__name__)

#: 设备更新走轻量路径（不重建 Protocol 连接）所允许的变更字段集。
_LIGHTWEIGHT_DEVICE_FIELDS = frozenset({"point_table", "device_group"})


def _changed_fields(old: DeviceConfig, new: DeviceConfig) -> set[str]:
    """返回两个设备配置间取值不同的字段名集合。"""
    old_dump = old.model_dump()
    new_dump = new.model_dump()
    return {k for k in old_dump if old_dump[k] != new_dump[k]}


class RuntimeMetricsPort(Protocol):
    """运行时指标端口——由组合根注入（接 Prometheus 计数器/直方图）。

    只承载「事件发生时累加」的计数与观测（connect 失败、重连成功、
    collect 完成、poll 时序统计）；gauge 类状态（设备连通数、sink 队列
    深度）由 ``/metrics`` 拉取时从 Runtime 快照覆盖，不经本端口。
    application 层不直接依赖 infra 的 metrics 模块——保持与引擎回调
    一致的依赖倒置。
    """

    def acquisition_run_finished(
        self, device_id: str, group: str, outcome: str, duration: float | None
    ) -> None:
        """一次 collect 结束；``outcome`` ∈ ``{"success", "partial", "failed"}``。"""
        ...

    def acquisition_poll_stats(
        self, device_id: str, group: str, jitter: float, overrun: bool, missed: int
    ) -> None:
        """一次 fixed-rate poll 的时序统计。

        ``jitter`` = 实际启动时刻 − 计划时刻（monotonic 口径）；``overrun``
        表示本轮结束越过了下一个计划时刻；``missed`` 为本次跳过的完整周期
        数（catch-up 至多一次，其余槽位跳过）。
        """
        ...

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        """一次 connect 尝试失败（启动 / 热增 / 重建 / 重连节流窗口内）。"""
        ...

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        """断线设备经 ensure 路径重连成功（驱动内部自重连不经 Runtime，不计入）。"""
        ...


def _is_connection_level(exc: BaseException) -> bool:
    """判定异常是否属于「连接级」故障（对端不可达的日常表现）。

    沿 ``__cause__`` 链检查：设备驱动通常把底层 ``OSError`` /
    ``TimeoutError`` 包装成 ``ProtocolError`` 再抛出（如 IEC104 的
    TCP connect 失败），只看最外层类型会漏判，因此穿透包装链。
    ``ConnectionRefusedError`` 是 ``OSError`` 的子类，一并覆盖。
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError | ConnectionRefusedError | OSError):
            return True
        current = current.__cause__
    return False


class Runtime:
    """运行时——组件注册表、生命周期与状态的唯一权威。

    注入依赖（构造期均为纯内存装配，无网络 I/O）：

    - ``devices`` — 设备注册表（``{device_id: Device}``），与
      ``CommandDispatcher`` 共享同一 dict，热重载就地增删后双方立即可见；
    - ``sinks`` — Sink 注册表；
    - ``engine`` — 采集引擎；本类构造时向其绑定 Sink 派发端口；
    - ``tasks`` — 采集 Task Definition 注册表（``{task_id: config}``）；
    - ``dispatcher`` — 命令分发器（持有以便组合根单点管理生命周期）；
    - ``config`` — ``RuntimeConfig``（队列容量、背压策略、超时）；
    - ``protocol_factory`` / ``sink_factory`` — 热重载重建组件用的工厂
      （由组合根注入，Runtime 不依赖具体适配器）。
    """

    def __init__(
        self,
        devices: dict[str, Device],
        sinks: dict[str, SinkPort],
        engine: AcquisitionEngine,
        dispatcher: CommandDispatcher,
        config: RuntimeConfig,
        tasks: dict[str, CollectionTaskConfig] | None = None,
        protocol_factory: Callable[[DeviceConfig], ProtocolPort] | None = None,
        sink_factory: Callable[[SinkConfig], SinkPort] | None = None,
        clock: Callable[[], float] = time.monotonic,
        metrics_hook: RuntimeMetricsPort | None = None,
    ) -> None:
        self._devices = devices
        self._sinks = sinks
        self._engine = engine
        self._dispatcher = dispatcher
        self._config = config
        self._task_defs: dict[str, CollectionTaskConfig] = dict(tasks or {})
        self._protocol_factory = protocol_factory
        self._sink_factory = sink_factory
        self._clock = clock
        # 运行时指标端口（可选）：组合根接 Prometheus；未注入时跳过计数。
        self._metrics = metrics_hook

        # 每台设备的运行状态（与 Device 分离——Device 聚合配置/点表/协议，
        # 状态随采集/重连演进）。设备增删/重建时同步维护。
        self._device_states: dict[str, DeviceRuntimeState] = {
            device_id: DeviceRuntimeState() for device_id in devices
        }

        # 采集 Task 运行时三簿记：
        # - ``_task_instances``：展开后的 Task Instance 快照（不可变，热重载
        #   整体替换）；
        # - ``_instance_states``：实例生命周期状态（RUNNING/STOPPED，显式
        #   簿记——acquisition handle 是否存在由它决定，不反向推断）；
        # - ``_acquisition_handles``：运行中实例的采集句柄（polling 协程
        #   或协议订阅，Runtime 不感知机制），不留 orphan 资源。
        self._task_instances: dict[str, CollectionTaskInstance] = {}
        self._instance_states: dict[str, TaskInstanceState] = {}
        self._acquisition_handles: dict[str, AcquisitionHandle] = {}
        # 热重载过程中需要恢复 RUNNING、但上一次重建失败的实例。
        # 下一次 reload 会继续重试，直到成功或实例被显式停止/删除。
        self._restart_pending: set[str] = set()

        # 每个采集实例的业务执行状态——与设备连接状态、实例启停状态分维度。
        # 实例注册时建立、注销时删除；引擎 collect 经 AcquisitionStatePort
        # 上报演进。
        self._acq_states: dict[str, AcquisitionRuntimeState] = {}

        self._lifecycle = RuntimeLifecycle(self)
        self._sink_dispatcher = RuntimeSinkDispatcher(self)

        # Sink 派发的落点：引擎采集结果进入本类的队列/背压/消费者机制。
        self._engine.attach_sink_dispatch(self)
        # 设备连接状态的落点：引擎采集前经 ensure_connected 完成带节流的
        # 重连，采集后上报 read 结果（本类实现 DeviceStatePort）。
        self._engine.attach_device_state(self)
        # 采集执行状态的落点：引擎上报每次 collect 的开始/成功/失败
        # （本类实现 AcquisitionStatePort）。
        self._engine.attach_acquisition_state(self)

        # 每个 Sink 使用独立有界 queue，容量来自 RuntimeConfig。
        self._queues: dict[str, asyncio.Queue[list[PointValue]]] = {
            name: asyncio.Queue(maxsize=config.queue_maxsize) for name in sinks
        }

        # Sink 消费者任务簿记
        self._sink_tasks: dict[str, asyncio.Task[None]] = {}
        # 生命周期串行化：start / stop 不能重叠，保证启动中状态不会被停机
        # 直接覆写；``running`` 依然只在 ``_started`` 真正完成后才返回 True。
        self._lifecycle_lock = asyncio.Lock()
        self._running = False
        self._started = False

        # start() 阶段 open 失败的 Sink 不启动 consumer，并由 health() 持续暴露为 unhealthy。
        self._unhealthy_sinks: set[str] = set()

        # 运行期统计：派发/丢弃在 Sink 派发侧计数；
        # 采集计数在引擎侧（经 ``points_collected`` 属性透传）。
        self._points_routed = 0
        self._points_dropped = 0

    # ------------------------------------------------------------------
    # 组件只读视图（QueryUseCase / 适配器经此读取当前实例，热重载安全）
    # ------------------------------------------------------------------

    def attach_metrics_hook(
        self,
        metrics_hook: RuntimeMetricsPort | None,
    ) -> None:
        """注入或替换可选 RuntimeMetricsPort。

        Args:
            metrics_hook: 外部宿主提供的指标 observer；None 表示关闭事件指标。

        Notes:
            这是组合边界，不改变采集、控制或队列行为。
        """
        self._metrics = metrics_hook

    @property
    def devices(self) -> dict[str, Device]:
        """当前设备注册表（热重载后就地反映最新内容）。"""
        return self._devices

    @property
    def sinks(self) -> dict[str, SinkPort]:
        """当前 Sink 注册表（热重载后就地反映最新内容）。"""
        return self._sinks

    @property
    def engine(self) -> AcquisitionEngine:
        """当前采集引擎（观察者注册、采集计数的入口）。"""
        return self._engine

    @property
    def dispatcher(self) -> CommandDispatcher:
        """命令分发器。"""
        return self._dispatcher

    # ------------------------------------------------------------------
    # Runtime 整体生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动运行时——连接设备、打开 sink、注册采集 Task Instance（默认
        STOPPED，显式 start 才启动 acquisition）。"""
        await self._lifecycle.start()

    async def stop(self) -> None:
        """优雅停机——关闭全部 acquisition handle、排空队列、flush 并关闭
        sink、关闭设备连接。"""
        await self._lifecycle.stop()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def health(self) -> dict[str, HealthStatus]:
        """返回全部设备与 sink 的健康状态（设备优先、随后 sink）。"""
        result: dict[str, HealthStatus] = {}
        for device_id, device in self._devices.items():
            result[device_id] = device.health()
        for name, sink in self._sinks.items():
            if name in self._unhealthy_sinks:
                result[name] = HealthStatus(healthy=False, message="open failed")
            else:
                result[name] = sink.health()
        return result

    @property
    def running(self) -> bool:
        """运行时是否完整就绪。

        ``True`` 仅在 :meth:`start` 完成全部步骤（设备连接尝试、sink
        打开、Task Instance 注册）之后、:meth:`stop` 开始之前。启动进行中
        （如不可达设备仍在 ``connect_timeout`` 内）为 ``False``，使
        ``/health`` 能区分「启动中」与「已就绪」。
        """
        return self._running and self._started

    @property
    def device_count(self) -> int:
        """返回当前 Runtime 注册的设备数量。"""
        return len(self._devices)

    @property
    def sink_count(self) -> int:
        """返回当前 Runtime 注册的 Sink 数量。"""
        return len(self._sinks)

    @property
    def points_collected(self) -> int:
        """累计采集点数（引擎侧口径）。"""
        return self._engine.points_collected

    @property
    def points_routed(self) -> int:
        """累计派发点数——成功进入 sink 队列的点值总数（单调不减）。"""
        return self._points_routed

    @property
    def points_dropped(self) -> int:
        """累计丢弃点数——背压策略丢弃的点值总数（单调不减）。"""
        return self._points_dropped

    def sink_queue_depths(self) -> dict[str, int]:
        """各 sink 队列当前深度——``/metrics`` 拉取时覆盖 ``sink_queue_depth``
        gauge（队列归 Runtime 所有，SinkPort 自身不感知队列）。"""
        return {name: queue.qsize() for name, queue in self._queues.items()}

    # ------------------------------------------------------------------
    # 采集 Task——定义查询与实例生命周期（TaskUseCase 的操作面）
    # ------------------------------------------------------------------

    def task_definitions(self) -> dict[str, CollectionTaskConfig]:
        """当前 Task Definition 注册表（浅拷贝）。"""
        return dict(self._task_defs)

    def task_instances(self) -> dict[str, CollectionTaskInstance]:
        """当前展开后的 Task Instance 注册表（浅拷贝）。"""
        return dict(self._task_instances)

    def instance_states(self) -> dict[str, TaskInstanceState]:
        """各 Task Instance 的生命周期状态（浅拷贝）。"""
        return dict(self._instance_states)

    async def start_task_instance(self, instance_id: str) -> None:
        """启动单个 Task Instance 的持续采集（经 Device 获取采集句柄）。

        幂等：已 RUNNING 的实例不触碰——同一实例绝不会出现两份 handle。
        采集机制（fixed-rate polling / 协议订阅）由 Device 按协议
        capability 决定，本方法不感知。

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
        device = self._devices.get(instance.device_id)
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

    async def stop_task_instance(self, instance_id: str) -> None:
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

    async def _sync_task_instances(
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
            self._acq_states.pop(iid, None)
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
                needs_restart = (
                    iid in self._restart_pending
                    or (
                        was_running
                        and (
                            old.interval != instance.interval
                            or old.point_group != instance.point_group
                            or instance.device_id in restart_subscription_devices
                        )
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
                        await self.start_task_instance(iid)
                    except Exception:
                        logger.warning(
                            "Task instance '%s' failed to restart acquisition after reload",
                            iid,
                            exc_info=True,
                        )
                        raise
                    else:
                        self._restart_pending.discard(iid)
            state = self._acq_states.get(iid)
            if (
                state is None
                or state.task_id != instance.task_id
                or state.point_group != instance.point_group
            ):
                self._acq_states[iid] = AcquisitionRuntimeState(
                    instance_id=iid,
                    task_id=instance.task_id,
                    device_id=instance.device_id,
                    point_group=instance.point_group,
                )

    def _desired_instances(self) -> dict[str, CollectionTaskInstance]:
        """展开当前 Task 定义为 Task Instance 集。

        - ``enabled: false`` 的 Task 不展开（不创建运行实例）；
        - ``device`` Task 只在设备存在且 enabled 时展开；
        - ``device_group`` Task 对每台 enabled 且 ``device_group`` 匹配的
          设备展开一个实例（disabled 设备不参与周期采集）。
        """
        desired: dict[str, CollectionTaskInstance] = {}
        for task in self._task_defs.values():
            if not task.enabled:
                continue
            if task.device is not None:
                dev = self._devices.get(task.device)
                device_ids = [task.device] if dev is not None and dev.enabled else []
            else:
                device_ids = [
                    d.device_id
                    for d in self._devices.values()
                    if d.enabled and d.device_group == task.device_group
                ]
            for device_id in device_ids:
                iid = task_instance_id(task.task_id, device_id)
                desired[iid] = CollectionTaskInstance(
                    instance_id=iid,
                    task_id=task.task_id,
                    device_id=device_id,
                    point_group=task.point_group,
                    interval=task.interval,
                    targets=[t.sink for t in task.targets],
                )
        return desired

    # ------------------------------------------------------------------
    # Sink 派发端口实现（AcquisitionEngine → Runtime 的落点）
    # ------------------------------------------------------------------

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """把按 sink 分组的批次入队，应用背压策略（实现 ``SinkDispatchPort``）。"""
        await self._sink_dispatcher.dispatch(routed)

    # ------------------------------------------------------------------
    # 设备状态端口实现（AcquisitionEngine → Runtime 的采集前/后钩子）
    # ------------------------------------------------------------------

    async def ensure_connected(self, device_id: str, *, force: bool = False) -> bool:
        """采集前确保设备可用，必要时按节流窗口重连（实现 ``DeviceStatePort``）。

        语义：

        - ``force=False`` 且 Runtime 状态已连接 → 立即 ``True``（零开销快路径）；
        - 断线但未到 ``next_retry_at`` 且 ``force=False`` → ``False``，本次采集跳过——
          高频轮询不会形成 connect 风暴；
        - ``force=True`` 用于显式控制/诊断请求：若 Runtime 状态显示已连接，
          还会检查协议 Driver health；health 不健康时忽略重连节流窗口并立即
          执行一次幂等 ``connect()``；
        - 断线且节流窗口已到 → 尝试一次 ``connect()``：成功则状态恢复
          （失败计数清零），失败则按指数 backoff 推迟下次窗口
          （1 s → 2 s → … → 30 s 封顶）。

        本方法只调用协议实例的 ``connect``——驱动内部若已自带
        重连监控（ADS/Modbus/IEC104 均有），``connect`` 的幂等实现会让
        重复调用安全收敛。
        """
        device = self._devices.get(device_id)
        if device is None:
            return False
        state = self._state_for(device_id)

        if state.connected:
            if not force:
                return True
            try:
                health = device.health()
            except Exception:
                health = HealthStatus(healthy=False, message="health check failed")
            if health.healthy:
                return True
            logger.info(
                "Device '%s' runtime state is connected but protocol health is unhealthy; "
                "forcing reconnect",
                device_id,
            )

        now = self._clock()
        if not force and now < state.next_retry_at:
            return False
        try:
            await asyncio.wait_for(device.connect(), timeout=self._config.connect_timeout)
        except TimeoutError:
            self._note_connect_failure(device_id, TimeoutError("connect timeout"))
            logger.warning(
                "connect timeout: device=%s timeout=%.1fs — reconnect attempt failed",
                device_id,
                self._config.connect_timeout,
            )
            return False
        except Exception as exc:
            self._note_connect_failure(device_id, exc)
            if _is_connection_level(exc):
                logger.warning("Device '%s' reconnect attempt failed: %s", device_id, exc)
            else:
                logger.warning("Device '%s' reconnect attempt failed", device_id, exc_info=True)
            return False
        state.mark_success(now)
        if self._metrics is not None:
            self._metrics.device_reconnected(device_id, self._protocol_name(device_id))
        logger.info("Device '%s' reconnected", device_id)
        return True

    def report_read_success(self, device_id: str) -> None:
        """采集读成功（实现 ``DeviceStatePort``）——状态恢复 connected。"""
        self._state_for(device_id).mark_success(self._clock())

    def report_read_failure(self, device_id: str, error: BaseException) -> None:
        """采集读失败（实现 ``DeviceStatePort``）。

        连接级失败标记断线（下一次 ``ensure_connected`` 起走重连节流）；
        协议/编程级失败只记录错误——连接本身可能仍然健康。
        """
        self._state_for(device_id).mark_read_failure(
            self._clock(), error, connection_level=_is_connection_level(error)
        )

    def device_state(self, device_id: str) -> DeviceRuntimeState | None:
        """返回设备当前运行状态（QueryUseCase 聚合 status 用）。"""
        return self._device_states.get(device_id)

    def _state_for(self, device_id: str) -> DeviceRuntimeState:
        """取设备运行状态；缺失时惰性创建（引擎只对已注册设备调用）。"""
        return self._device_states.setdefault(device_id, DeviceRuntimeState())

    def _note_connect_failure(self, device_id: str, exc: BaseException) -> None:
        """集中记账一次 connect 失败：更新设备状态并上报指标。"""
        self._state_for(device_id).mark_connect_failure(self._clock(), exc)
        if self._metrics is not None:
            self._metrics.device_connect_failed(device_id, self._protocol_name(device_id))

    def _protocol_name(self, device_id: str) -> str:
        """设备协议名（指标标签用）；设备已从注册表移除时回退 'unknown'。"""
        device = self._devices.get(device_id)
        return device.config.protocol if device is not None else "unknown"

    # ------------------------------------------------------------------
    # 采集执行状态端口实现（AcquisitionEngine → Runtime 的 collect 钩子）
    # ------------------------------------------------------------------

    def report_collect_started(self, execution_id: str, device_id: str, group: str) -> None:
        """一次 collect 开始（实现 ``AcquisitionStatePort``）。"""
        self._acq_state_for(execution_id, device_id, group).begin(self._clock())

    def report_collect_success(
        self, execution_id: str, device_id: str, group: str, *, partial: bool
    ) -> None:
        """一次 collect 成功（含 partial——GOOD/BAD 混合不计连续失败）。"""
        state = self._acq_state_for(execution_id, device_id, group)
        state.finish_success(self._clock(), partial=partial)
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

    def acquisition_states(self) -> dict[str, AcquisitionRuntimeState]:
        """当前采集实例执行状态簿（``{instance_id: state}`` 浅拷贝，
        QueryUseCase 用）。"""
        return dict(self._acq_states)

    def _acq_state_for(
        self, execution_id: str, device_id: str, group: str
    ) -> AcquisitionRuntimeState:
        """取采集执行状态；缺失时按当前实例信息创建（引擎只对运行中实例
        上报，实例必然已注册）。"""
        state = self._acq_states.get(execution_id)
        if state is None:
            inst = self._task_instances[execution_id]
            state = AcquisitionRuntimeState(
                instance_id=execution_id,
                task_id=inst.task_id,
                device_id=device_id,
                point_group=group,
            )
            self._acq_states[execution_id] = state
        return state

    # ------------------------------------------------------------------
    # 热重载——设备管理
    # ------------------------------------------------------------------

    async def add_device(
        self,
        device_id: str,
        cfg: DeviceConfig,
        protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """运行时新增设备；重复应用同一目标配置时安全收敛。

        部分 reload 失败后下一次会重放同一 diff。若设备已经按目标配置存在，
        不重复替换协议实例，只立即重试连接；若同 ID 但配置不同，则走 rebuild。
        """
        existing = self._devices.get(device_id)
        if existing is not None:
            if existing.config.model_dump() == cfg.model_dump() and existing.points == points:
                await self.ensure_connected(device_id, force=True)
                await self._sync_task_instances()
                return
            await self.rebuild_device(device_id, cfg, protocol, points)
            return

        device = Device(config=cfg, points=points, protocol=protocol)
        self._devices[device_id] = device
        self._device_states[device_id] = DeviceRuntimeState()

        try:
            await asyncio.wait_for(device.connect(), timeout=self._config.connect_timeout)
            self._device_states[device_id].mark_success(self._clock())
            logger.info("Hot-reload: device '%s' connected", device_id)
        except TimeoutError:
            self._note_connect_failure(device_id, TimeoutError("connect timeout"))
            logger.warning(
                "connect timeout: device=%s timeout=%.1fs — hot-reload connect failed",
                device_id,
                self._config.connect_timeout,
            )
        except Exception as exc:
            self._note_connect_failure(device_id, exc)
            logger.warning(
                "Hot-reload: device '%s' failed to connect",
                device_id,
                exc_info=True,
            )

        # 新设备可能落入某些 device_group Task 的展开范围。
        await self._sync_task_instances()

    async def remove_device(self, device_id: str) -> None:
        """运行时移除设备——停止并注销其全部采集实例并关闭连接。"""
        device = self._devices.pop(device_id, None)
        if device is not None:
            try:
                await device.close()
            except Exception:
                logger.warning(
                    "Hot-reload: device '%s' close failed",
                    device_id,
                    exc_info=True,
                )

        self._device_states.pop(device_id, None)
        await self._sync_task_instances()
        logger.info("Hot-reload: device '%s' removed", device_id)

    async def rebuild_device(
        self,
        device_id: str,
        new_cfg: DeviceConfig,
        new_protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """重建设备——关闭旧连接，换入新配置/驱动后重新接入。

        正在运行的相关 TaskInstance：先关闭旧采集句柄，Device 重建完成
        后重新启动原本 RUNNING 的实例，保持其原运行状态。
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

        old_device = self._devices.pop(device_id, None)
        if old_device is not None:
            try:
                await old_device.close()
            except Exception:
                logger.warning(
                    "Hot-reload: old protocol close failed for '%s'",
                    device_id,
                    exc_info=True,
                )

        device = Device(config=new_cfg, points=points, protocol=new_protocol)
        self._devices[device_id] = device
        # 驱动实例已更换——运行状态随之重置（新驱动的首次 connect 结果
        # 立即写入全新状态）。
        self._device_states[device_id] = DeviceRuntimeState()

        try:
            await asyncio.wait_for(device.connect(), timeout=self._config.connect_timeout)
            self._device_states[device_id].mark_success(self._clock())
            logger.info("Hot-reload: device '%s' reconnected", device_id)
        except TimeoutError:
            self._note_connect_failure(device_id, TimeoutError("connect timeout"))
            logger.warning(
                "connect timeout: device=%s timeout=%.1fs — connect failed after rebuild",
                device_id,
                self._config.connect_timeout,
            )
        except Exception as exc:
            self._note_connect_failure(device_id, exc)
            logger.warning(
                "Hot-reload: device '%s' connect failed after rebuild",
                device_id,
                exc_info=True,
            )

        # device_group / enabled 可能随新配置变化——重新展开采集实例。
        await self._sync_task_instances()

        # 恢复原本 RUNNING 的实例（仍存在于此设备的展开结果中）。
        for iid in was_running:
            if iid not in self._task_instances:
                continue
            try:
                await self.start_task_instance(iid)
            except Exception:
                logger.warning(
                    "Hot-reload: task instance '%s' failed to restart after device rebuild",
                    iid,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # 热重载——sink 管理
    # ------------------------------------------------------------------

    async def add_sink(self, sink_name: str, cfg: SinkConfig, sink: SinkPort) -> None:
        """运行时新增 sink；open 成功后才提交到 Runtime 注册表。

        部分 reload 失败重试时，若同名 sink 已存在，直接按 rebuild 路径
        收敛到目标实例，避免重复注册消费者或遗留半初始化对象。

        Raises:
            Exception: ``sink.open()`` 失败原样上抛；失败前不修改注册表。
        """
        if sink_name in self._sinks:
            await self.rebuild_sink(sink_name, cfg, sink)
            return

        await sink.open()
        queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=self._config.queue_maxsize)
        self._sinks[sink_name] = sink
        self._queues[sink_name] = queue
        logger.info("Hot-reload: sink '%s' opened", sink_name)

        if self._running:
            task = asyncio.create_task(self._sink_consumer(sink_name, sink))
            self._sink_tasks[sink_name] = task

    async def remove_sink(self, sink_name: str) -> None:
        """运行时移除 sink——停消费者、flush、关闭、移除队列。"""
        task = self._sink_tasks.pop(sink_name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

        old_sink = self._sinks.pop(sink_name, None)
        if old_sink is not None:
            try:
                await old_sink.flush()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' flush failed",
                    sink_name,
                    exc_info=True,
                )
            try:
                await old_sink.close()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' close failed",
                    sink_name,
                    exc_info=True,
                )

        self._queues.pop(sink_name, None)
        self._unhealthy_sinks.discard(sink_name)
        logger.info("Hot-reload: sink '%s' removed", sink_name)

    async def rebuild_sink(self, sink_name: str, new_cfg: SinkConfig, new_sink: SinkPort) -> None:
        """重建 sink——先打开新实例，成功后再切换旧实例。

        既有队列保留，避免在途数据丢失。新 sink 打开失败时旧 sink 与消费者
        完全保持不变，使 reload 可以安全重试。

        Raises:
            Exception: 新 sink 的 ``open()`` 失败原样上抛。
        """
        await new_sink.open()

        task = self._sink_tasks.pop(sink_name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

        old_sink = self._sinks.get(sink_name)
        if old_sink is not None:
            try:
                await old_sink.flush()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' flush failed during rebuild",
                    sink_name,
                    exc_info=True,
                )
            try:
                await old_sink.close()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' close failed during rebuild",
                    sink_name,
                    exc_info=True,
                )

        self._sinks[sink_name] = new_sink
        self._unhealthy_sinks.discard(sink_name)
        logger.info("Hot-reload: sink '%s' re-opened", sink_name)

        if self._running:
            new_task = asyncio.create_task(self._sink_consumer(sink_name, new_sink))
            self._sink_tasks[sink_name] = new_task

    # ------------------------------------------------------------------
    # 热重载编排（ConfigUseCase 的唯一入口）
    # ------------------------------------------------------------------

    def convergence_diff(self, target: Config) -> ConfigDiff:
        """基于真实 Runtime 注册表生成强制收敛 diff。

        用于失败回滚或 revision reconciliation。它不依赖 ConfigUseCase 的
        current_config 基线，而是按当前实际设备/Sink/Task 注册表与目标配置
        生成一个保守 diff，确保曾被部分 reconfigure 修改的运行态能够重新
        收敛到目标配置。
        """
        target_devices = {item.device_id: item for item in target.devices.devices}
        actual_device_ids = set(self._devices)
        target_device_ids = set(target_devices)
        devices = DeviceDiff(
            added=sorted(target_device_ids - actual_device_ids),
            removed=sorted(actual_device_ids - target_device_ids),
            updated=sorted(target_device_ids & actual_device_ids),
        )

        target_sinks = {item.name: item for item in target.system.sinks}
        actual_sink_ids = set(self._sinks)
        target_sink_ids = set(target_sinks)
        sinks = SinkDiff(
            added=sorted(target_sink_ids - actual_sink_ids),
            removed=sorted(actual_sink_ids - target_sink_ids),
            updated=sorted(target_sink_ids & actual_sink_ids),
        )

        target_task_ids = {item.task_id for item in target.tasks.tasks}
        actual_task_ids = set(self._task_defs)
        tasks = TaskDiff(
            added=sorted(target_task_ids - actual_task_ids),
            removed=sorted(actual_task_ids - target_task_ids),
            updated=sorted(target_task_ids & actual_task_ids),
        )

        changed_tables = sorted(target.point_tables.tables)
        return ConfigDiff(
            devices=devices,
            sinks=sinks,
            tasks=tasks,
            points_changed=bool(changed_tables),
            point_tables_changed=changed_tables,
            units_changed=True,
        )

    async def reconfigure(self, new_config: Config, diff: ConfigDiff) -> list[str]:
        """按 diff 重构运行时——设备/sink/task 增删重建与点表重注入。

        各阶段相互隔离：单阶段失败记录到返回的错误列表，其余阶段继续执行。
        本方法不修改配置快照——``current_config`` 的提交时机由
        ConfigUseCase 决定。

        Args:
            new_config: 已加载并通过校验的新配置。
            diff: 新旧配置的 diff（由 ConfigUseCase 计算）。

        Returns:
            错误描述列表；空列表表示全部阶段成功。
        """
        errors: list[str] = []

        # 在设备配置被就地更新前记录“point_table 绑定发生变化”的订阅设备。
        # 这类变化即使两张表内容本身都未修改，也必须重新注册 notification。
        restart_subscription_devices: set[str] = set()
        new_devices = {d.device_id: d for d in new_config.devices.devices}
        for did in diff.devices.updated:
            old_device = self._devices.get(did)
            new_device = new_devices.get(did)
            if (
                old_device is not None
                and new_device is not None
                and old_device.config.point_table != new_device.point_table
                and _changed_fields(old_device.config, new_device)
                <= _LIGHTWEIGHT_DEVICE_FIELDS
                and old_device.acquisition_mode is AcquisitionMode.SUBSCRIBE
            ):
                restart_subscription_devices.add(did)

        try:
            await self._apply_device_diff(diff, new_config)
        except Exception as exc:
            logger.error("Device diff apply failed: %s", exc, exc_info=True)
            errors.append(f"device: {exc}")

        try:
            await self._apply_sink_diff(diff, new_config)
        except Exception as exc:
            logger.error("Sink diff apply failed: %s", exc, exc_info=True)
            errors.append(f"sink: {exc}")

        # 点表内容变化：对绑定受影响表、且未在设备 diff 中增删重建的设备，
        # 仅重注入点映射——不重建 Protocol 连接。
        if diff.point_tables_changed:
            try:
                self._reinject_changed_tables(new_config, diff)
            except Exception as exc:
                logger.error("Point mapping re-inject failed: %s", exc, exc_info=True)
                errors.append(f"points: {exc}")

        # Task 定义变化：整体替换注册表并重新展开实例。设备/点表变化也可能
        # 改变展开结果（device_group 成员、enabled 翻转），统一在此收尾同步
        # ——实例增删只影响对应实例，不触碰任何 Protocol 连接。
        if diff.point_tables_changed:
            changed_tables = set(diff.point_tables_changed)
            for did, device in self._devices.items():
                if (
                    device.config.point_table in changed_tables
                    and device.acquisition_mode is AcquisitionMode.SUBSCRIBE
                ):
                    restart_subscription_devices.add(did)

        try:
            self._task_defs = {t.task_id: t for t in new_config.tasks.tasks}
            await self._sync_task_instances(
                restart_subscription_devices=restart_subscription_devices
            )
        except Exception as exc:
            logger.error("Task instance sync failed: %s", exc, exc_info=True)
            errors.append(f"tasks: {exc}")

        return errors

    def _reinject_changed_tables(self, new_config: Config, diff: ConfigDiff) -> None:
        """点表内容变化时，对绑定受影响表的既有设备重注入点映射。

        仅重注入内存映射（``Device.set_points``——点表 + 协议映射），
        不触碰 Protocol 连接。新增/删除设备跳过；updated 设备若已经成功
        收敛到目标配置则仍会重注入，避免“轻量设备字段变化 + 点表内容变化”
        时遗漏新 mapping。
        """
        changed_tables = set(diff.point_tables_changed)
        removed = set(diff.devices.removed)
        added = set(diff.devices.added)
        target_devices = {d.device_id: d for d in new_config.devices.devices}

        for did, device in self._devices.items():
            if did in removed or did in added:
                continue
            target = target_devices.get(did)
            if target is None:
                continue
            # 设备更新阶段若未成功收敛到目标配置，不在这里继续叠加点表变化；
            # 成功的轻量更新/重建以及未更新设备都可安全重注入当前目标点表。
            if device.config.model_dump() != target.model_dump():
                continue
            if device.config.point_table not in changed_tables:
                continue
            device.set_points(self._points_for_device(new_config, did))
            logger.info(
                "Hot-reload: point mapping re-injected for '%s' (table '%s')",
                did,
                device.config.point_table,
            )

    async def _apply_device_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        """按 diff 增删重建设备；新增/重建的协议实例由工厂创建。

        仅 ``point_table`` / ``device_group`` 变化的设备走轻量路径——就地
        更新配置、按需重注入点映射，不重建 Protocol 连接。
        """
        new_devices = {d.device_id: d for d in new_cfg.devices.devices}

        lightweight: set[str] = set()
        for did in diff.devices.updated:
            old_device = self._devices.get(did)
            if old_device is not None and _changed_fields(old_device.config, new_devices[did]) <= (
                _LIGHTWEIGHT_DEVICE_FIELDS
            ):
                lightweight.add(did)

        factory = self._protocol_factory
        if factory is None and (diff.devices.added or set(diff.devices.updated) - lightweight):
            raise RuntimeError("protocol factory is not wired into Runtime")

        for did in diff.devices.removed:
            await self.remove_device(did)

        for did in diff.devices.added:
            cfg = new_devices[did]
            # 入口已守卫：有新增/非轻量更新时 factory 必然非 None
            assert factory is not None
            protocol = factory(cfg)
            await self.add_device(did, cfg, protocol, self._points_for_device(new_cfg, did))

        for did in diff.devices.updated:
            cfg = new_devices[did]
            if did in lightweight:
                self._apply_lightweight_device_update(did, cfg, new_cfg)
                continue
            assert factory is not None  # 同上——入口守卫保证
            protocol = factory(cfg)
            await self.rebuild_device(did, cfg, protocol, self._points_for_device(new_cfg, did))

    def _apply_lightweight_device_update(
        self, device_id: str, new_dev: DeviceConfig, new_cfg: Config
    ) -> None:
        """轻量设备更新（仅 point_table / device_group 变化）——不重建连接。

        - ``point_table`` 变化：仅向既有协议重注入新点映射；
        - ``device_group`` 变化：不触碰连接，仅影响 ``device_group`` Task
          的展开结果（由 ``reconfigure`` 末尾的统一同步处理）。
        """
        device = self._devices[device_id]
        old_dev = device.config
        device.config = new_dev

        if new_dev.point_table != old_dev.point_table:
            device.set_points(self._points_for_device(new_cfg, device_id))

        logger.info("Hot-reload: device '%s' updated in place (no reconnect)", device_id)

    @staticmethod
    def _points_for_device(config: Config, device_id: str) -> list[PointConfig]:
        """取设备绑定点表中的点列表（设备无关点表经绑定解析）。"""
        return config.points_for_device(device_id)

    async def _apply_sink_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        """按 diff 增删重建 sink；新实例由工厂创建。"""
        factory = self._sink_factory
        if factory is None and (diff.sinks.added or diff.sinks.updated):
            raise RuntimeError("sink factory is not wired into Runtime")
        new_sinks = {s.name: s for s in new_cfg.system.sinks}

        for name in diff.sinks.removed:
            await self.remove_sink(name)

        for name in diff.sinks.added:
            cfg = new_sinks[name]
            # 入口已守卫：有新增/更新时 factory 必然非 None
            assert factory is not None
            sink = factory(cfg)
            await self.add_sink(name, cfg, sink)

        for name in diff.sinks.updated:
            cfg = new_sinks[name]
            assert factory is not None  # 同上——入口守卫保证
            sink = factory(cfg)
            await self.rebuild_sink(name, cfg, sink)

    # ------------------------------------------------------------------
    # 私有——sink 背压与消费者
    # ------------------------------------------------------------------

    async def _sink_consumer(self, sink_name: str, sink: SinkPort) -> None:
        """Per-sink 消费者任务——从队列取批次并写入 SinkPort。"""
        queue = self._queues[sink_name]
        try:
            while True:
                batch = await queue.get()
                if not batch:  # empty list = shutdown sentinel
                    break
                try:
                    await sink.write(batch)
                except Exception:
                    logger.warning(
                        "Sink '%s' write failed for %d points",
                        sink_name,
                        len(batch),
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.info("Sink consumer '%s' cancelled", sink_name)
            raise
