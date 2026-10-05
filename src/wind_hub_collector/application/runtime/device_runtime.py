"""Collector 设备会话、连接状态与协议生命周期的唯一权威。

只管理设备子系统；Task、采集句柄与 Sink 的启停顺序由 CollectorRuntime 协调。
采集引擎经 DeviceStatePort 在原有读前/读后时机调用本对象，不增加重连调度。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from wind_hub_collector.application.runtime.device import CollectorDeviceSession
from wind_hub_collector.application.runtime.device_state import DeviceRuntimeState
from wind_hub_core.config import DeviceConfig, PointConfig, RuntimeConfig
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.protocol.port import ProtocolPort

if TYPE_CHECKING:
    from wind_hub_collector.application.runtime.runtime import RuntimeMetricsPort

logger = logging.getLogger(__name__)


#: 设备更新走轻量路径（不重建 Protocol 连接）所允许的变更字段集。
_LIGHTWEIGHT_DEVICE_FIELDS = frozenset({"point_table", "device_group"})


def _changed_fields(old: DeviceConfig, new: DeviceConfig) -> set[str]:
    """返回两个设备配置间取值不同的字段名集合。"""
    old_dump = old.model_dump()
    new_dump = new.model_dump()
    return {k for k in old_dump if old_dump[k] != new_dump[k]}


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


class DeviceRuntime:
    """持有设备会话与连接状态，并负责连接、重连及设备热更新。

    接管传入的会话注册表；不保存 Task、采集句柄或 Sink 状态。协议工厂仅依赖
    ProtocolPort 抽象，连接超时、重试窗口和指标上报沿用 Collector 的既有策略。
    """

    def __init__(
        self,
        devices: dict[str, CollectorDeviceSession],
        config: RuntimeConfig,
        protocol_factory: Callable[[DeviceConfig], ProtocolPort] | None = None,
        clock: Callable[[], float] = time.monotonic,
        metrics_hook: RuntimeMetricsPort | None = None,
    ) -> None:
        self._devices = devices
        self._device_states = {device_id: DeviceRuntimeState() for device_id in devices}
        self._config = config
        self._protocol_factory = protocol_factory
        self._clock = clock
        self._metrics = metrics_hook

    @property
    def devices(self) -> dict[str, CollectorDeviceSession]:
        """当前会话注册表；调用方只读，生命周期变更经本对象的方法执行。"""
        return self._devices

    def attach_metrics_hook(self, metrics_hook: RuntimeMetricsPort | None) -> None:
        """与 Collector 的指标 observer 同步替换，不改变连接状态。"""
        self._metrics = metrics_hook

    def health(self) -> dict[str, HealthStatus]:
        """按注册顺序读取设备协议健康状态。"""
        return {device_id: device.health() for device_id, device in self._devices.items()}

    def require_protocol_factory(self) -> None:
        """在设备 diff 执行任何删除前检查工厂，保持原有失败边界。"""
        if self._protocol_factory is None:
            raise RuntimeError("protocol factory is not wired into Runtime")

    def create_protocol(self, config: DeviceConfig) -> ProtocolPort:
        """按已校验配置创建协议；调用方随后协调采集句柄并交回设备增建操作。"""
        self.require_protocol_factory()
        assert self._protocol_factory is not None
        return self._protocol_factory(config)

    def requires_rebuild(
        self, device_id: str, config: DeviceConfig, points: list[PointConfig]
    ) -> bool:
        """重复热增遇到不同配置/点表时，通知协调器先停旧会话的采集句柄。"""
        existing = self._devices.get(device_id)
        return existing is not None and (
            existing.config.model_dump() != config.model_dump()
            or existing.points != tuple(points)
        )

    def is_lightweight_update(self, device_id: str, config: DeviceConfig) -> bool:
        """仅点表绑定/设备分组变化时可保留现有协议连接。"""
        existing = self._devices.get(device_id)
        return existing is not None and (
            _changed_fields(existing.config, config) <= _LIGHTWEIGHT_DEVICE_FIELDS
        )

    def update_device(
        self,
        device_id: str,
        config: DeviceConfig,
        resolve_points: Callable[[], list[PointConfig]],
    ) -> None:
        """就地应用轻量配置；先换配置，再仅在绑定改变时解析并注入点表。

        保留原有点表解析时机与失败语义；device_group 变化不触碰协议连接。
        Task 展开由调用方在整体 reconfigure 收尾时同步。
        """
        device = self._devices[device_id]
        old_config = device.config
        device.config = config
        if config.point_table != old_config.point_table:
            device.set_points(resolve_points())
        logger.info("Hot-reload: device '%s' updated in place (no reconnect)", device_id)

    def set_points(self, device_id: str, points: list[PointConfig]) -> None:
        """重注入已收敛设备的点映射，不重建协议连接。"""
        device = self._devices[device_id]
        device.set_points(points)
        logger.info(
            "Hot-reload: point mapping re-injected for '%s' (table '%s')",
            device_id,
            device.config.point_table,
        )

    async def connect_all(self) -> None:
        """启动时逐台连接，单设备失败记账后继续（best-effort）。"""
        # 点映射已在装配期（组合根 / add_device / rebuild_device）注入
        # 到各 Device 的协议实例，这里只做连接。
        for device_id, device in self._devices.items():
            try:
                await asyncio.wait_for(
                    device.connect(),
                    timeout=self._config.connect_timeout,
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
                    self._config.connect_timeout,
                )
            except Exception as exc:
                self._note_connect_failure(device_id, exc)
                if _is_connection_level(exc):
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
        """停机时逐台关闭；关闭失败不阻断其他设备释放，保留注册表。"""
        for device_id, device in self._devices.items():
            try:
                await device.close()
            except Exception:
                logger.warning("Device '%s' close failed", device_id, exc_info=True)

    async def ensure_connected(self, device_id: str, *, force: bool = False) -> bool:
        """采集前确保设备可用，必要时按节流窗口重连（实现 ``DeviceStatePort``）。

        语义：

        - ``force=False`` 且 DeviceRuntime 状态已连接 → 立即 ``True``（零开销快路径）；
        - 断线但未到 ``next_retry_at`` 且 ``force=False`` → ``False``，本次采集跳过——
          高频轮询不会形成 connect 风暴；
        - ``force=True`` 用于显式控制/诊断请求：若 DeviceRuntime 状态显示已连接，
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

        优先沿异常 cause 链判定连接级故障；若第三方协议库把断线包装成
        不含 OSError 的协议异常，则再读取 Driver 的缓存 health。Driver 已
        标记 unhealthy 时同样把 DeviceRuntime 状态切到 disconnected，使下一周期
        进入 ``ensure_connected`` 重连路径。
        """
        connection_level = _is_connection_level(error)
        if not connection_level:
            device = self._devices.get(device_id)
            if device is not None:
                try:
                    connection_level = not device.health().healthy
                except Exception:
                    logger.debug(
                        "Device '%s' health check failed while classifying read error",
                        device_id,
                        exc_info=True,
                    )
        self._state_for(device_id).mark_read_failure(
            self._clock(), error, connection_level=connection_level
        )

    def device_state(self, device_id: str) -> DeviceRuntimeState | None:
        """返回设备当前运行状态（CollectorQueryService 聚合 status 用）。"""
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
            same_config = existing.config.model_dump() == cfg.model_dump()
            same_points = existing.points == tuple(points)
            if same_config and same_points:
                await self.ensure_connected(device_id, force=True)
                return
            await self.rebuild_device(device_id, cfg, protocol, points)
            return

        device = CollectorDeviceSession(config=cfg, points=points, protocol=protocol)
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

    async def rebuild_device(
        self,
        device_id: str,
        new_cfg: DeviceConfig,
        new_protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """重建设备——关闭旧连接，换入新配置/驱动后重新接入。

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

        device = CollectorDeviceSession(config=new_cfg, points=points, protocol=new_protocol)
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
