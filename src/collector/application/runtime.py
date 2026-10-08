"""CollectorRuntime —— 采集系统三个子 Runtime 之间的整体协调器。

架构位置：application 层。职责：

- 设备子系统——委托 :class:`DeviceRuntime` 管理会话、连接状态与协议生命周期；
- Task 子系统——委托 :class:`TaskRuntime` 管理 Task Definition 展开、实例
  启停、采集句柄与采集执行状态；
- Sink 子系统——委托 :class:`SinkRuntime` 管理 Sink 生命周期、队列、
  消费者、背压与派发；
- 设备与 Sink 的增删 / 重建，配置热重载时的跨子系统顺序（:meth:`reconfigure`）；
- CollectorRuntime 状态（running / 聚合 health / 组件计数 / 点位统计）；
- 整体 ``start()`` / ``stop()`` 的子系统顺序。

不负责：协议实现细节（ProtocolPort 适配器）、配置加载与 diff
（CollectorConfigService）、采集时序（acquisition handle）、Sink 交付细节
（SinkRuntime）。

失败语义：设备连接与 sink 打开均为 best-effort——单个失败记录日志并跳过，
其余组件照常启动，失败组件经 :meth:`health` 暴露为不健康。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from core.application import ConnectionHealth
from core.domain import DeviceId

from ..domain.acquisition import AcquisitionEngine
from .acquisition_state import AcquisitionRuntimeState
from .config import CollectionTask, CollectorConfig, DeviceView, RuntimeParams
from .device_runtime import DeviceRuntime
from .device_state import DeviceRuntimeState
from .reload import (
    HOT_RELOADABLE_RUNTIME_FIELDS,
    ConfigDiff,
    DeviceDiff,
    SinkDiff,
    TaskDiff,
)
from .session import AcquisitionMode, CollectorDeviceSession
from .sink_port import SinkFactory, SinkPort
from .sink_runtime import SinkRuntime
from .task_instance import CollectionTaskInstance, TaskInstanceState
from .task_runtime import TaskRuntime

logger = logging.getLogger(__name__)


class RuntimeMetricsPort(Protocol):
    """运行时指标端口——由组合根注入（接 Prometheus 计数器/直方图）。

    只承载「事件发生时累加」的计数与观测（connect 失败、重连成功、
    collect 完成、poll 时序统计）；gauge 类状态（设备连通数、sink 队列
    深度）由指标拉取时从 CollectorRuntime 快照覆盖，不经本端口。
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
        """断线设备经 ensure 路径重连成功（驱动内部自重连不经 CollectorRuntime，不计入）。"""
        ...


#: 设备会话工厂——由组合根注入（ProtocolRegistry.create + 会话装配）。
SessionFactory = Callable[[DeviceView], CollectorDeviceSession]


class CollectorRuntime:
    """Collector 整体生命周期协调器——Device/Task/Sink 三个子 Runtime 的装配与编排。

    注入依赖（构造期均为纯内存装配，无网络 I/O）：

    - ``devices`` — 初始设备会话注册表与其创建视图，构造时把所有权交给
      DeviceRuntime；
    - ``sinks`` — Sink 注册表，构造时把所有权交给 SinkRuntime；
    - ``engine`` — 采集引擎；本类构造时把三个状态端口分别绑定到对应
      子 Runtime（设备 → DeviceRuntime，采集 → TaskRuntime，派发 → SinkRuntime）；
    - ``tasks`` — 采集 Task Definition 注册表，构造时把所有权交给 TaskRuntime；
    - ``params`` — 运行时参数（队列容量、背压策略、超时）；
    - ``session_factory`` / ``sink_factory`` — 热重载重建组件用的工厂
      （由组合根注入）。
    """

    def __init__(
        self,
        devices: dict[str, CollectorDeviceSession],
        device_views: dict[str, DeviceView],
        sinks: dict[str, SinkPort],
        engine: AcquisitionEngine,
        params: RuntimeParams,
        tasks: dict[str, CollectionTask] | None = None,
        session_factory: SessionFactory | None = None,
        sink_factory: SinkFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        metrics_hook: RuntimeMetricsPort | None = None,
    ) -> None:
        self._device_runtime = DeviceRuntime(devices, params, clock, metrics_hook)
        for device_id, view in device_views.items():
            self._device_runtime.register_view(device_id, view)
        self._task_runtime = TaskRuntime(
            tasks or {}, self._device_runtime, engine, clock, metrics_hook
        )
        self._sink_runtime = SinkRuntime(sinks, params, sink_factory)
        self._session_factory = session_factory
        self._engine = engine
        self._params = params

        # 设备连接状态的落点：引擎采集前经 ensure_connected 完成带节流的
        # 重连，采集后上报 read 结果；设备端口直接绑定唯一 owner。
        self._engine.attach_device_state(self._device_runtime)
        # 采集执行状态的落点：引擎上报每次 collect 的开始/成功/失败
        # （TaskRuntime 实现 AcquisitionStatePort）。
        self._engine.attach_acquisition_state(self._task_runtime)
        # Sink 派发的落点：引擎采集结果进入 SinkRuntime 的队列/背压/消费者机制。
        self._engine.attach_sink_dispatch(self._sink_runtime)

        # 生命周期串行化：start / stop 不能重叠，保证启动中状态不会被停机
        # 直接覆写；``running`` 依然只在 ``_started`` 真正完成后才返回 True。
        self._lifecycle_lock = asyncio.Lock()
        self._running = False
        self._started = False

    # ------------------------------------------------------------------
    # 组件只读视图（查询/控制服务经此读取当前实例，热重载安全）
    # ------------------------------------------------------------------

    def attach_metrics_hook(
        self,
        metrics_hook: RuntimeMetricsPort | None,
    ) -> None:
        """注入或替换可选 RuntimeMetricsPort（组合边界，不改变采集行为）。"""
        self._device_runtime.attach_metrics_hook(metrics_hook)
        self._task_runtime.attach_metrics_hook(metrics_hook)

    @property
    def device_runtime(self) -> DeviceRuntime:
        """设备子系统；设备查询与连接状态端口的唯一入口。"""
        return self._device_runtime

    @property
    def task_runtime(self) -> TaskRuntime:
        """Task 子系统；Task 查询、实例生命周期与采集执行状态的唯一入口。"""
        return self._task_runtime

    @property
    def sink_runtime(self) -> SinkRuntime:
        """Sink 子系统；Sink 注册表、队列、消费者与派发端口的唯一入口。"""
        return self._sink_runtime

    @property
    def devices(self) -> Mapping[str, CollectorDeviceSession]:
        """当前设备注册表的只读视图（所有权与变更均在 DeviceRuntime）。"""
        return self._device_runtime.devices

    @property
    def sinks(self) -> Mapping[str, SinkPort]:
        """当前 Sink 注册表的只读视图（所有权与变更均在 SinkRuntime）。"""
        return self._sink_runtime.sinks

    def device_state(self, device_id: str) -> DeviceRuntimeState | None:
        """单台设备的连接/重连簿记状态（委托 DeviceRuntime）。"""
        return self._device_runtime.device_state(device_id)

    def device_health(self) -> dict[str, ConnectionHealth]:
        """全部设备的健康状态（委托 DeviceRuntime）。"""
        return self._device_runtime.health()

    def sink_health(self) -> dict[str, ConnectionHealth]:
        """全部 Sink 的健康状态（委托 SinkRuntime）。"""
        return self._sink_runtime.health()

    @property
    def engine(self) -> AcquisitionEngine:
        """当前采集引擎（观察者注册、采集计数的入口）。"""
        return self._engine

    # ------------------------------------------------------------------
    # CollectorRuntime 整体生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动运行时——连接设备、打开 sink、注册采集 Task Instance（默认
        STOPPED，显式 start 才启动 acquisition）。

        子系统顺序：设备连接 → Sink 打开/消费者 → Task Instance 注册。
        单设备/单 Sink 失败只记录状态并继续，避免一个现场端点阻断整进程。
        """
        async with self._lifecycle_lock:
            if self._running:
                return
            self._running = True
            self._started = False

            await self._device_runtime.connect_all()
            await self._sink_runtime.start()
            # 注册采集 Task Instance（默认 STOPPED）——程序启动不自动开始
            # 采集，只有控制面的显式 start 才启动 acquisition。
            await self._task_runtime.sync_instances()

            self._started = True

    async def stop(self) -> None:
        """优雅停机——关闭全部 acquisition handle、排空队列、flush 并关闭
        sink、关闭设备连接。

        子系统按依赖逆序：先停采集（不再产生数据），再排空/关闭 Sink，
        最后关闭设备连接。单资源清理异常被记录但不阻断其他资源释放。
        """
        async with self._lifecycle_lock:
            if not self._running:
                return
            self._running = False
            self._started = False

            # 关闭全部实例采集句柄——polling 协程取消、订阅注销，不留
            # 后台 task 或订阅；实例定义保留并统一标记 STOPPED。
            await self._task_runtime.stop_all()
            await self._sink_runtime.stop()
            await self._device_runtime.close_all()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def health(self) -> dict[str, ConnectionHealth]:
        """返回全部设备与 sink 的健康状态（设备优先、随后 sink）。"""
        result = self._device_runtime.health()
        result.update(self._sink_runtime.health())
        return result

    @property
    def running(self) -> bool:
        """运行时是否完整就绪。

        ``True`` 仅在 :meth:`start` 完成全部步骤（设备连接尝试、sink
        打开、Task Instance 注册）之后、:meth:`stop` 开始之前。启动进行中
        （如不可达设备仍在 ``connect_timeout`` 内）为 ``False``，使
        健康检查能区分「启动中」与「已就绪」。
        """
        return self._running and self._started

    @property
    def device_count(self) -> int:
        """返回当前 CollectorRuntime 注册的设备数量。"""
        return len(self._device_runtime.devices)

    @property
    def sink_count(self) -> int:
        """返回当前 CollectorRuntime 注册的 Sink 数量。"""
        return len(self._sink_runtime.sinks)

    @property
    def points_collected(self) -> int:
        """累计采集点数（引擎侧口径）。"""
        return self._engine.points_collected

    @property
    def points_routed(self) -> int:
        """累计派发点数——成功进入 sink 队列的点值总数（SinkRuntime 口径）。"""
        return self._sink_runtime.points_routed

    @property
    def points_dropped(self) -> int:
        """累计丢弃点数——背压策略丢弃的点值总数（SinkRuntime 口径）。"""
        return self._sink_runtime.points_dropped

    def sink_queue_depths(self) -> dict[str, int]:
        """各 sink 队列当前深度（只读透传 SinkRuntime）。"""
        return self._sink_runtime.queue_depths()

    # ------------------------------------------------------------------
    # 采集 Task——查询与实例生命周期委托（实现与簿记在 TaskRuntime）
    # ------------------------------------------------------------------

    def task_definitions(self) -> dict[str, CollectionTask]:
        """当前 Task Definition 注册表（浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.task_definitions()

    def task_instances(self) -> dict[str, CollectionTaskInstance]:
        """当前展开后的 Task Instance 注册表（浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.task_instances()

    def instance_states(self) -> dict[str, TaskInstanceState]:
        """各 Task Instance 的生命周期状态（浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.instance_states()

    async def start_task_instance(self, instance_id: str) -> None:
        """启动单个 Task Instance 的持续采集（委托 TaskRuntime）。

        Raises:
            KeyError: ``instance_id`` 不存在。
            Exception: 采集启动失败——状态保持 STOPPED，错误原样上抛。
        """
        await self._task_runtime.start_instance(instance_id)

    async def stop_task_instance(self, instance_id: str) -> None:
        """停止单个 Task Instance 的持续采集（委托 TaskRuntime）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        await self._task_runtime.stop_instance(instance_id)

    def acquisition_states(self) -> dict[str, AcquisitionRuntimeState]:
        """当前采集实例执行状态簿（``{instance_id: state}`` 浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.acquisition_states()

    # ------------------------------------------------------------------
    # 热重载——设备管理
    # ------------------------------------------------------------------

    def _require_session_factory(self) -> SessionFactory:
        """在设备 diff 执行任何删除前检查工厂，保持原有失败边界。"""
        if self._session_factory is None:
            raise RuntimeError("session factory is not wired into Runtime")
        return self._session_factory

    async def add_device(self, device_id: str, view: DeviceView) -> None:
        """运行时新增设备；重复应用同一目标配置时安全收敛。

        部分 reload 失败后下一次会重放同一 diff。若设备已经按目标配置存在，
        不重复替换协议实例，只立即重试连接；若同 ID 但配置不同，则走 rebuild。
        """
        if self._device_runtime.requires_rebuild(device_id, view):
            await self.rebuild_device(device_id, view)
            return
        if device_id in self._device_runtime.devices:
            # 已按目标配置存在——立即重试连接后收敛。
            await self._device_runtime.ensure_connected(device_id, force=True)
            await self._task_runtime.sync_instances()
            return
        factory = self._require_session_factory()
        await self._device_runtime.add_device(device_id, view, factory(view))
        # 新设备或重放热增可能影响 device_group Task 的展开。
        await self._task_runtime.sync_instances()

    async def remove_device(self, device_id: str) -> None:
        """运行时移除设备——停止并注销其全部采集实例并关闭连接。"""
        await self._device_runtime.remove_device(device_id)
        await self._task_runtime.sync_instances()
        logger.info("Hot-reload: device '%s' removed", device_id)

    async def rebuild_device(self, device_id: str, view: DeviceView) -> None:
        """重建设备——关闭旧连接，换入新会话后重新接入。

        正在运行的相关 TaskInstance：先关闭旧采集句柄，会话重建完成后
        重新启动原本 RUNNING 的实例，保持其原运行状态。
        """
        factory = self._require_session_factory()
        was_running = await self._task_runtime.suspend_for_device_change(device_id)

        await self._device_runtime.rebuild_device(device_id, view, factory(view))

        # device_group 可能随新配置变化——重新展开采集实例。
        await self._task_runtime.sync_instances()

        # 恢复原本 RUNNING 的实例（仍存在于此设备的展开结果中）。
        await self._task_runtime.resume_after_device_change(was_running)

    # ------------------------------------------------------------------
    # 热重载编排（CollectorConfigService 的唯一入口）
    # ------------------------------------------------------------------

    def convergence_diff(self, target: CollectorConfig) -> ConfigDiff:
        """基于真实 CollectorRuntime 注册表生成强制收敛 diff。

        用于失败回滚或 revision reconciliation。它不依赖 CollectorConfigService
        的 current_config 基线，而是按当前实际设备/Sink/Task 注册表与目标配置
        生成一个保守 diff，确保曾被部分 reconfigure 修改的运行态能够重新
        收敛到目标配置。
        """
        target_device_ids = {str(d) for d in target.devices}
        actual_device_ids = set(self._device_runtime.devices)
        devices = DeviceDiff(
            added=sorted(target_device_ids - actual_device_ids),
            removed=sorted(actual_device_ids - target_device_ids),
            updated=sorted(target_device_ids & actual_device_ids),
        )

        target_sinks = {name: sink for name, sink in target.sinks.items() if sink.enabled}
        actual_sink_ids = set(self._sink_runtime.sinks)
        target_sink_ids = set(target_sinks)
        sinks = SinkDiff(
            added=sorted(target_sink_ids - actual_sink_ids),
            removed=sorted(actual_sink_ids - target_sink_ids),
            updated=sorted(target_sink_ids & actual_sink_ids),
        )

        target_task_ids = set(target.tasks)
        actual_task_ids = set(self._task_runtime.task_definitions())
        tasks = TaskDiff(
            added=sorted(target_task_ids - actual_task_ids),
            removed=sorted(actual_task_ids - target_task_ids),
            updated=sorted(target_task_ids & actual_task_ids),
        )

        changed_tables = sorted(str(t) for t in target.point_tables)
        return ConfigDiff(
            devices=devices,
            sinks=sinks,
            tasks=tasks,
            points_changed=bool(changed_tables),
            point_tables_changed=changed_tables,
            runtime_changed=any(
                getattr(self._params, name) != getattr(target.runtime, name)
                for name in HOT_RELOADABLE_RUNTIME_FIELDS
            ),
        )

    async def reconfigure(self, new_config: CollectorConfig, diff: ConfigDiff) -> list[str]:
        """按 diff 重构运行时——设备/sink/task 增删重建与点表重注入。

        各阶段相互隔离：单阶段失败记录到返回的错误列表，其余阶段继续执行。
        本方法不修改配置快照——``current_config`` 的提交时机由
        CollectorConfigService 决定。

        Args:
            new_config: 已加载并通过校验的新配置。
            diff: 新旧配置的 diff（由 CollectorConfigService 计算）。

        Returns:
            错误描述列表；空列表表示全部阶段成功。
        """
        errors: list[str] = []

        # 在设备配置被就地更新前记录“point_table 绑定发生变化”的订阅设备。
        # 这类变化即使两张表内容本身都未修改，也必须重新注册 notification。
        restart_subscription_devices: set[str] = set()
        for did in diff.devices.updated:
            device_id = DeviceId(did)
            old_view = self._device_runtime.view_of(did)
            new_view = new_config.device_view(device_id)
            session = self._device_runtime.devices.get(did)
            if (
                old_view is not None
                and session is not None
                and old_view.point_table.point_table_id != new_view.point_table.point_table_id
                and self._device_runtime.is_lightweight_update(did, new_view)
                and session.acquisition_mode is AcquisitionMode.SUBSCRIBE
            ):
                restart_subscription_devices.add(did)

        try:
            await self._apply_device_diff(diff, new_config)
        except Exception as exc:
            logger.error("Device diff apply failed: %s", exc, exc_info=True)
            errors.append(f"device: {exc}")

        try:
            await self._sink_runtime.apply_diff(diff.sinks, new_config.sinks)
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
        # 改变展开结果（device_group 成员变化），统一在此收尾同步——实例
        # 增删只影响对应实例，不触碰任何 Protocol 连接。
        if diff.point_tables_changed:
            changed_tables = set(diff.point_tables_changed)
            for did, session in self._device_runtime.devices.items():
                if (
                    str(session.point_table.point_table_id) in changed_tables
                    and session.acquisition_mode is AcquisitionMode.SUBSCRIBE
                ):
                    restart_subscription_devices.add(did)

        # 可热更新的 RuntimeParams 字段变化：把新 params 快照应用到各
        # 子系统 owner（restart-required 字段已在 prepare 阶段被拒绝）。
        if diff.runtime_changed:
            try:
                self.apply_runtime_params(new_config.runtime)
            except Exception as exc:
                logger.error("Runtime params apply failed: %s", exc, exc_info=True)
                errors.append(f"runtime: {exc}")

        try:
            await self._task_runtime.apply_definitions(
                dict(new_config.tasks),
                restart_subscription_devices=restart_subscription_devices,
            )
        except Exception as exc:
            logger.error("Task instance sync failed: %s", exc, exc_info=True)
            errors.append(f"tasks: {exc}")

        return errors

    def apply_runtime_params(self, params: RuntimeParams) -> None:
        """把可热更新的 RuntimeParams 快照应用到各子系统 owner。

        只替换 params 引用——热更新字段（``backpressure_policy`` /
        ``shutdown_timeout`` / ``connect_timeout``）由各 owner 在使用点
        动态读取，替换即生效；``queue_maxsize`` / ``read_timeout`` 为
        restart-required，热重载 prepare 阶段已拒绝其变化。
        """
        self._params = params
        self._device_runtime.update_params(params)
        self._sink_runtime.update_params(params)

    def _reinject_changed_tables(self, new_config: CollectorConfig, diff: ConfigDiff) -> None:
        """点表内容变化时，对绑定受影响表的既有设备重注入点映射。

        仅重注入内存映射（会话 ``set_points``——点表 + 元数据），不触碰
        Protocol 连接。新增/删除设备跳过；updated 设备若已经成功收敛到
        目标配置则仍会重注入，避免“轻量设备字段变化 + 点表内容变化”时
        遗漏新 mapping。
        """
        changed_tables = set(diff.point_tables_changed)
        removed = set(diff.devices.removed)
        added = set(diff.devices.added)

        for did, session in self._device_runtime.devices.items():
            if did in removed or did in added:
                continue
            if DeviceId(did) not in new_config.devices:
                # 部分失败残留：目标配置已无此设备，等下一轮 diff 收敛。
                continue
            target_view = new_config.device_view(DeviceId(did))
            current_view = self._device_runtime.view_of(did)
            # 设备更新阶段若未成功收敛到目标配置，不在这里继续叠加点表变化；
            # 成功的轻量更新/重建以及未更新设备都可安全重注入当前目标点表。
            if current_view is not None and not _views_match(current_view, target_view):
                continue
            if str(session.point_table.point_table_id) not in changed_tables:
                continue
            self._device_runtime.set_points(did, target_view)

    async def _apply_device_diff(self, diff: ConfigDiff, new_cfg: CollectorConfig) -> None:
        """按 diff 增删重建设备；新增/重建的会话由工厂创建。

        仅点表绑定/设备分组变化的设备走轻量路径——就地更新配置、重注入
        点映射，不重建 Protocol 连接。
        """
        lightweight: set[str] = set()
        for did in diff.devices.updated:
            if self._device_runtime.is_lightweight_update(did, new_cfg.device_view(DeviceId(did))):
                lightweight.add(did)

        if diff.devices.added or set(diff.devices.updated) - lightweight:
            self._require_session_factory()

        for did in diff.devices.removed:
            await self.remove_device(did)

        for did in diff.devices.added:
            await self.add_device(did, new_cfg.device_view(DeviceId(did)))

        for did in diff.devices.updated:
            view = new_cfg.device_view(DeviceId(did))
            if did in lightweight:
                self._device_runtime.update_device(did, view)
                continue
            await self.rebuild_device(did, view)


def _views_match(current: DeviceView, target: DeviceView) -> bool:
    """设备是否已收敛到目标视图（与 reload.compute_diff 的设备签名同口径）。"""
    return (
        current.device == target.device
        and dict(current.options) == dict(target.options)
        and current.point_table.point_table_id == target.point_table.point_table_id
        and current.subscribe_enabled == target.subscribe_enabled
        and current.supports_scheduled_collection == target.supports_scheduled_collection
    )
