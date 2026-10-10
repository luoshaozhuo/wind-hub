"""Collector 设备会话、连接状态与协议生命周期的唯一权威。

只管理设备子系统；Task、采集句柄与 Sink 的启停顺序由 CollectorRuntime
协调。采集引擎经 Domain ``DeviceStatePort`` 在原有读前/读后时机调用
本对象，不增加重连调度。

会话实例的创建（协议工厂 + 点表注入）由组合根/CollectorRuntime 经
``session_factory`` 完成；本对象只接管已创建会话的生命周期。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from core.application import ConnectionHealth
from core.application.recovery import RecoveringProtocol

from .config import DeviceView, RuntimeParams
from .device_state import DeviceRuntimeState
from .session import CollectorDeviceSession

if TYPE_CHECKING:
    from .runtime import RuntimeMetricsPort

logger = logging.getLogger(__name__)


def is_connection_level(exc: BaseException) -> bool:
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


class DeviceRuntime:
    """持有设备会话与连接状态，并负责连接、重连及设备热更新。

    接管传入的会话注册表；不保存 Task、采集句柄或 Sink 状态。连接超时、
    重试窗口和指标上报沿用 Collector 的既有策略。
    """

    def __init__(
        self,
        devices: dict[str, CollectorDeviceSession],
        params: RuntimeParams,
        clock: Callable[[], float] = time.monotonic,
        metrics_hook: RuntimeMetricsPort | None = None,
    ) -> None:
        self._devices = devices
        self._device_states = {device_id: DeviceRuntimeState() for device_id in devices}
        self._params = params
        self._clock = clock
        self._metrics = metrics_hook
        # 每台设备的 connect 串行化锁：并发 ensure_connected（多个采集实例、
        # force 控制路径）在同一断线事件上只允许一个协程真正发起 connect，
        # 其余协程在锁内复查状态后直接收敛，避免重复连接与重复指标记账。
        self._connect_locks: dict[str, asyncio.Lock] = {}
        # 每台设备建会话时使用的 DeviceView（轻量更新判定的比较基线）。
        self._views: dict[str, DeviceView] = {}
        # RecoveringProtocol 在读路径内透明重连时对上层不可见——把重连事件接回
        # 运行状态与指标，保证 device_reconnects 如实反映每一次实际重连。
        for device_id, device in devices.items():
            self._wire_reconnect_hook(device_id, device)

    @property
    def devices(self) -> Mapping[str, CollectorDeviceSession]:
        """当前会话注册表的只读视图（随热重载就地反映最新内容）。

        本对象是注册表的唯一 owner——外部只能观察，生命周期变更必须经
        本对象的方法执行。
        """
        return MappingProxyType(self._devices)

    def update_params(self, params: RuntimeParams) -> None:
        """热更新运行时参数快照——``connect_timeout`` 在每次 connect 动态读取。"""
        self._params = params

    def health(self) -> dict[str, ConnectionHealth]:
        """按注册顺序读取设备协议健康状态。"""
        return {device_id: device.health() for device_id, device in self._devices.items()}

    # ------------------------------------------------------------------
    # 热更新判定（CollectorRuntime 在 reconfigure 时调用）
    # ------------------------------------------------------------------

    def register_view(self, device_id: str, view: DeviceView) -> None:
        """登记设备会话的创建视图（启动装配与 add/rebuild 后调用）。"""
        self._views[device_id] = view

    def requires_rebuild(self, device_id: str, view: DeviceView) -> bool:
        """重复热增遇到不同配置/点表时，通知协调器先停旧会话的采集句柄。"""
        existing = self._views.get(device_id)
        if device_id not in self._devices or existing is None:
            return device_id in self._devices
        return _view_signature(existing) != _view_signature(view)

    def is_lightweight_update(self, device_id: str, view: DeviceView) -> bool:
        """仅点表绑定/设备分组/名称变化时可保留现有协议连接。"""
        existing = self._views.get(device_id)
        if device_id not in self._devices or existing is None:
            return False
        return (
            existing.device.endpoint == view.device.endpoint
            and dict(existing.options) == dict(view.options)
            and existing.device.device_model_id == view.device.device_model_id
            and existing.subscribe_enabled == view.subscribe_enabled
            and existing.supports_scheduled_collection == view.supports_scheduled_collection
        )

    def update_device(self, device_id: str, view: DeviceView) -> None:
        """就地应用轻量配置；先换设备快照，再仅在绑定改变时重注入点表。

        保留原有点表解析时机与失败语义；device_group 变化不触碰协议连接。
        Task 展开由调用方在整体 reconfigure 收尾时同步。
        """
        device = self._devices[device_id]
        device.set_device(view.device)
        # 点表绑定切换与元数据/表内容更新同路径——重注入内存映射。
        device.set_points(view.point_table, view.point_meta)
        self._views[device_id] = view
        logger.info("Hot-reload: device '%s' updated in place (no reconnect)", device_id)

    def set_points(self, device_id: str, view: DeviceView) -> None:
        """重注入已收敛设备的点映射/元数据，不重建协议连接。"""
        device = self._devices[device_id]
        device.set_points(view.point_table, view.point_meta)
        self._views[device_id] = view
        logger.info(
            "Hot-reload: point mapping re-injected for '%s' (table '%s')",
            device_id,
            view.point_table.point_table_id,
        )

    def view_of(self, device_id: str) -> DeviceView | None:
        """返回设备当前登记的创建视图（reconfigure 收敛判定用）。"""
        return self._views.get(device_id)

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def connect_all(self) -> None:
        """启动时逐台连接，单设备失败记账后继续（best-effort）。"""
        for device_id, device in self._devices.items():
            try:
                await asyncio.wait_for(
                    device.connect(),
                    timeout=self._params.connect_timeout,
                )
                self._state_for(device_id).mark_success(self._clock())
                logger.info("Device '%s' connected", device_id)
            except TimeoutError:
                self._note_connect_failure(
                    device_id,
                    TimeoutError("connect timeout"),
                )
                logger.warning(
                    "connect timeout: device=%s timeout=%.1fs — skipped",
                    device_id,
                    self._params.connect_timeout,
                )
            except Exception as exc:
                self._note_connect_failure(device_id, exc)
                if is_connection_level(exc):
                    logger.warning(
                        "Device '%s' failed to connect — skipped: %s",
                        device_id,
                        exc,
                    )
                else:
                    logger.warning(
                        "Device '%s' failed to connect — skipped",
                        device_id,
                        exc_info=True,
                    )

    async def close_all(self) -> None:
        """停机时逐台关闭；关闭失败不阻断其他设备释放，保留注册表。

        单台关闭以 ``connect_timeout`` 为上界：Driver close 内部若阻塞
        （如同步协议栈关闭挂起），不能让一台设备耗尽整体停机预算。
        """
        for device_id, device in self._devices.items():
            try:
                await asyncio.wait_for(device.close(), timeout=self._params.connect_timeout)
            except TimeoutError:
                logger.warning(
                    "Device '%s' close exceeded %.1fs — continuing with remaining devices",
                    device_id,
                    self._params.connect_timeout,
                )
            except Exception:
                logger.warning("Device '%s' close failed", device_id, exc_info=True)

    async def ensure_connected(self, device_id: str, *, force: bool = False) -> bool:
        """采集前确保设备可用，必要时按节流窗口重连（实现 ``DeviceStatePort``）。

        语义：

        - ``force=False`` 且 DeviceRuntime 状态已连接 → 立即 ``True``（零开销快路径）；
        - 断线但未到 ``next_retry_at`` 且 ``force=False`` → ``False``，本次采集跳过——
          高频轮询不会形成 connect 风暴；
        - ``force=True`` 用于显式控制/诊断请求：若 DeviceRuntime 状态显示已连接，
          还会检查协议传输是否打开（``is_open``）；传输已断开时忽略重连
          节流窗口并立即执行一次幂等 ``connect()``（可复用的连接不会被
          无意义重建）；
        - 断线且节流窗口已到 → 尝试一次 ``connect()``：成功则状态恢复
          （失败计数清零），失败则按指数 backoff 推迟下次窗口
          （1 s → 2 s → … → 30 s 封顶）。
        """
        device = self._devices.get(device_id)
        if device is None:
            return False
        state = self._state_for(device_id)

        if state.connected:
            if not force:
                return True
            if _transport_open(device):
                return True
            logger.info(
                "Device '%s' runtime state is connected but transport is not open; "
                "forcing reconnect",
                device_id,
            )

        now = self._clock()
        if not force and now < state.next_retry_at:
            return False

        async with self._connect_locks.setdefault(device_id, asyncio.Lock()):
            # 等锁期间设备可能被移除/重建——旧会话不再归本调用管理。
            if self._devices.get(device_id) is not device:
                return False
            # 等锁期间其他协程可能已完成重连（或透明重连回调已标记恢复）——
            # 复查状态，避免对同一次断线重复 connect、重复记账。
            state = self._state_for(device_id)
            if state.connected:
                if not force:
                    return True
                if _transport_open(device):
                    return True
            now = self._clock()
            if not force and now < state.next_retry_at:
                return False
            return await self._connect_locked(device_id, device, state)

    async def _connect_locked(
        self,
        device_id: str,
        device: CollectorDeviceSession,
        state: DeviceRuntimeState,
    ) -> bool:
        """在设备 connect 锁内执行一次带超时的 connect 并记账。"""
        now = self._clock()
        # 读路径的透明重连回调（sync hook）可能在 connect 等待期间抢先完成
        # 恢复并已记账——此时本次 connect 在 RecoveringProtocol 内幂等返回，
        # 不再重复计一次重连指标。
        pre_success_at = state.last_success_at
        try:
            await asyncio.wait_for(device.connect(), timeout=self._params.connect_timeout)
        except TimeoutError:
            self._note_connect_failure(device_id, TimeoutError("connect timeout"))
            logger.warning(
                "connect timeout: device=%s timeout=%.1fs — reconnect attempt failed",
                device_id,
                self._params.connect_timeout,
            )
            return False
        except Exception as exc:
            self._note_connect_failure(device_id, exc)
            if is_connection_level(exc):
                logger.warning("Device '%s' reconnect attempt failed: %s", device_id, exc)
            else:
                logger.warning("Device '%s' reconnect attempt failed", device_id, exc_info=True)
            return False
        recovered_by_hook = state.last_success_at != pre_success_at
        state.mark_success(now)
        if self._metrics is not None and not recovered_by_hook:
            self._metrics.device_reconnected(device_id, self._protocol_name(device_id))
        logger.info("Device '%s' reconnected", device_id)
        return True

    def report_read_success(self, device_id: str) -> None:
        """采集读成功（实现 ``DeviceStatePort``）——状态恢复 connected。"""
        self._state_for(device_id).mark_success(self._clock())

    def report_read_failure(self, device_id: str, error: BaseException) -> None:
        """采集读失败（实现 ``DeviceStatePort``）。

        优先沿异常 cause 链判定连接级故障；若第三方协议库把断线包装成
        不含 OSError 的协议异常，则再读取 Driver 的缓存 health。Driver 已
        标记 unhealthy 时同样把 DeviceRuntime 状态切到 disconnected，使下一周期
        进入 ``ensure_connected`` 重连路径。
        """
        connection_level = is_connection_level(error)
        if not connection_level:
            device = self._devices.get(device_id)
            if device is not None:
                connection_level = not _transport_open(device)
        self._state_for(device_id).mark_read_failure(
            self._clock(), error, connection_level=connection_level
        )

    def device_state(self, device_id: str) -> DeviceRuntimeState | None:
        """返回设备当前运行状态（查询服务聚合 status 用）。"""
        return self._device_states.get(device_id)

    # ------------------------------------------------------------------
    # 设备增删重建（会话实例由调用方经 session_factory 创建）
    # ------------------------------------------------------------------

    async def add_device(
        self, device_id: str, view: DeviceView, session: CollectorDeviceSession
    ) -> None:
        """运行时新增设备会话并立即尝试连接；失败记账后保留会话（周期重试）。

        调用方负责重复热增的收敛判定（``requires_rebuild``）与 Task 实例
        的 suspend/resume 协调。
        """
        self._devices[device_id] = session
        self._device_states[device_id] = DeviceRuntimeState()
        self._views[device_id] = view
        self._wire_reconnect_hook(device_id, session)
        await self._connect_new(device_id, session, "connected")

    async def remove_device(self, device_id: str) -> None:
        """运行时移除设备——关闭连接并同步移除设备及状态。"""
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
        self._views.pop(device_id, None)

    async def rebuild_device(
        self, device_id: str, view: DeviceView, session: CollectorDeviceSession
    ) -> None:
        """重建设备——关闭旧连接，换入新会话后重新接入。

        调用方负责先停止使用旧会话的采集句柄，再在返回后恢复实例。
        """
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

        self._devices[device_id] = session
        # 驱动实例已更换——运行状态随之重置（新驱动的首次 connect 结果
        # 立即写入全新状态）。
        self._device_states[device_id] = DeviceRuntimeState()
        self._views[device_id] = view
        self._wire_reconnect_hook(device_id, session)
        await self._connect_new(device_id, session, "reconnected")

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    async def _connect_new(
        self, device_id: str, session: CollectorDeviceSession, verb: str
    ) -> None:
        """新增/重建设备的首次连接尝试；失败记账但不移除会话。"""
        try:
            await asyncio.wait_for(session.connect(), timeout=self._params.connect_timeout)
            self._device_states[device_id].mark_success(self._clock())
            logger.info("Hot-reload: device '%s' %s", device_id, verb)
        except TimeoutError:
            self._note_connect_failure(device_id, TimeoutError("connect timeout"))
            logger.warning(
                "connect timeout: device=%s timeout=%.1fs — hot-reload connect failed",
                device_id,
                self._params.connect_timeout,
            )
        except Exception as exc:
            self._note_connect_failure(device_id, exc)
            logger.warning(
                "Hot-reload: device '%s' failed to connect",
                device_id,
                exc_info=True,
            )

    def _wire_reconnect_hook(self, device_id: str, session: CollectorDeviceSession) -> None:
        """把 RecoveringProtocol 读路径内透明重连事件接回运行状态与指标。

        回调与注册时的会话身份绑定：设备被移除/重建后，旧会话 RecoveringProtocol
        的迟到重连回调（在途读触发的恢复晚于新会话注册）不得改写新会话的
        运行状态或重复记账指标——以注册表中的会话身份做失效判定，无需额外
        generation 计数。
        """
        protocol = session.protocol
        if isinstance(protocol, RecoveringProtocol):

            def _on_reconnect(session: CollectorDeviceSession = session) -> None:
                if self._devices.get(device_id) is session:
                    self._on_transparent_reconnect(device_id)

            protocol.set_reconnect_hook(_on_reconnect)

    def _on_transparent_reconnect(self, device_id: str) -> None:
        """透明重连成功——恢复 connected 状态并如实记账一次重连。

        状态恢复使下一周期 ``ensure_connected`` 走快路径，避免对同一次
        断线重复 connect/重复计数。
        """
        self._state_for(device_id).mark_success(self._clock())
        if self._metrics is not None:
            self._metrics.device_reconnected(device_id, self._protocol_name(device_id))
        logger.info("Device '%s' reconnected (transparent reconnect in read path)", device_id)

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
        return device.protocol_name if device is not None else "unknown"


def _transport_open(device: CollectorDeviceSession) -> bool:
    """查询协议传输连接是否打开；查询失败按不可达处理。"""
    try:
        return device.is_open()
    except Exception:
        logger.debug("Device is_open check failed while classifying", exc_info=True)
        return False


def _view_signature(view: DeviceView) -> object:
    """设备视图的重建判定签名（与 reload.compute_diff 的设备签名同口径）。"""
    return (
        view.device,
        dict(view.options),
        view.point_table.point_table_id,
        view.subscribe_enabled,
        view.supports_scheduled_collection,
    )
