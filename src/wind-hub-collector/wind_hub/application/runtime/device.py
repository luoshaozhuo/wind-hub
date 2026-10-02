"""Device —— 运行时设备的唯一聚合对象。

架构位置：application/runtime。``Device`` 把一台设备运行所需的全部要素
聚合为单一权威（source of truth）：

- :class:`~wind_hub.config.schema.DeviceConfig`——静态配置快照；
- 解析后的点表（``list[PointConfig]``）——热重载可经 :meth:`set_points`
  就地更新；
- :class:`~wind_hub.domain.port.outbound.ProtocolPort`——协议运行实例。

Runtime 只持有 ``dict[str, Device]``，不再平行维护
``DeviceConfig`` / ``ProtocolPort`` / ``points_by_device`` 三套索引。

持续采集经 :meth:`Device.start_acquisition` 启动，返回
:class:`AcquisitionHandle`；协议采集机制（主动轮询 / 订阅推送）由
``ProtocolPort.acquisition_mode`` 决定，Task 与 Runtime 均不判断协议名：

- ``POLL``——本模块的 :class:`PollingAcquisitionHandle` 以 monotonic
  fixed-rate 调度调用方注入的 ``acquire`` 回调（读取与处理由
  AcquisitionEngine 完成）；
- ``SUBSCRIBE``——委托 ``ProtocolPort.subscribe``，数据到达即经
  ``on_data`` 回调交给 AcquisitionEngine；若协议实现
  :class:`~wind_hub.domain.port.outbound.InterrogationCapable`
  （IEC104 master），订阅建立后自动触发一次 General Interrogation。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from wind_hub_core.config.schema import DeviceConfig, PointConfig
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.point import PointRef, PointValue
from wind_hub_core.model.health import HealthStatus
from wind_hub.domain.port.outbound import (
    AcquisitionMode,
    InterrogationCapable,
    ProtocolPort,
    SubscriptionHandle,
)

logger = logging.getLogger(__name__)


class AcquisitionHandle(Protocol):
    """一次持续采集的句柄——关闭即停止采集，不影响同设备的其他实例。

    底层可能是 fixed-rate polling 协程、ADS notification 连接池或
    IEC104 订阅；Runtime 只依赖 ``close()``，不感知具体机制。
    """

    async def close(self) -> None:
        """停止采集并释放资源（必须幂等）。"""
        ...


class PollingAcquisitionHandle:
    """Fixed-rate 主动轮询句柄（monotonic clock + absolute deadline）。

    语义：

    - 采样时刻对齐 ``t0 + k·interval``——不是「上轮完成后 sleep
      interval」的 fixed-delay，因此不随读取耗时长漂；
    - 单实例串行执行 ``acquire``，绝不重入（每个 interval 最多启动一次）；
    - overrun（本轮结束越过下一个 deadline）：最多允许**一次**立即
      catch-up；连续错过多个周期时跳过多余槽位（不爆发补采），并重新
      对齐到后续未来 deadline；
    - 统计经 ``on_stats(jitter, overrun, missed)`` 上报——jitter 为
      「实际启动时刻 − 计划时刻」（monotonic 口径）。
    """

    def __init__(
        self,
        interval: float,
        acquire: Callable[[], Awaitable[None]],
        on_stats: Callable[[float, bool, int], None] | None = None,
    ) -> None:
        self._interval = interval
        self._acquire = acquire
        self._on_stats = on_stats
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """启动轮询协程（幂等——重复调用不产生第二个协程）。"""
        if self._task is None:
            self._closed = False
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """取消轮询协程并等待其退出（幂等）。

        先置 ``_closed`` 再 cancel：acquire 在途时协议库可能把取消
        转换成自家异常吞掉（pymodbus 的 “Request cancelled outside
        library”），task.cancel() 单独无法终止协程——``_closed``
        标志保证本轮 acquire 结束后循环退出，close 不会无限等待。
        """
        task, self._task = self._task, None
        if task is None:
            return
        self._closed = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_deadline = loop.time()
        while not self._closed:
            now = loop.time()
            if now < next_deadline:
                # 只在 event loop 上提前唤醒等待；真正的 acquire 不在
                # 理论 deadline 之前执行。
                await asyncio.sleep(next_deadline - now)
            scheduled = next_deadline
            actual = loop.time()
            try:
                await self._acquire()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单次采集失败不终止轮询——下一周期自然重试。
                logger.warning("Polling acquire failed — continuing next cycle", exc_info=True)
            end = loop.time()
            next_deadline = scheduled + self._interval
            overrun = end > next_deadline
            missed = 0
            if overrun:
                lag = end - next_deadline
                # 跳过的完整周期数；剩下的至多一个槽位允许立即 catch-up。
                missed = int(lag // self._interval)
                next_deadline += missed * self._interval
            if self._on_stats is not None:
                self._on_stats(actual - scheduled, overrun, missed)


class _SubscriptionAcquisitionHandle:
    """订阅式采集句柄——包装 ``ProtocolPort.subscribe`` 返回的订阅句柄。"""

    def __init__(self, subscription: SubscriptionHandle) -> None:
        self._subscription = subscription

    async def close(self) -> None:
        await self._subscription.close()


class Device:
    """运行时设备——配置、点表与协议实例的唯一聚合。

    热重载语义：

    - 点表变化：:meth:`set_points` 就地更新点表并重注入协议映射，
      不重建连接；
    - 协议/连接参数变化：由 Runtime 整体替换 ``Device`` 对象（本对象
      视为随协议实例同生命周期）。
    """

    def __init__(
        self,
        config: DeviceConfig,
        points: list[PointConfig],
        protocol: ProtocolPort,
    ) -> None:
        self._config = config
        self._points = points
        self._protocol = protocol

    # ------------------------------------------------------------------
    # 身份与配置
    # ------------------------------------------------------------------

    @property
    def config(self) -> DeviceConfig:
        """当前设备配置快照（热重载轻量更新时就地替换）。"""
        return self._config

    @config.setter
    def config(self, value: DeviceConfig) -> None:
        self._config = value

    @property
    def device_id(self) -> str:
        return self._config.device_id

    @property
    def device_group(self) -> str | None:
        return self._config.device_group

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def protocol(self) -> ProtocolPort:
        """协议运行实例（寻址与批量读写由其实现）。"""
        return self._protocol

    @property
    def points(self) -> list[PointConfig]:
        """当前解析后的点表。"""
        return self._points

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """持续采集能力——直接透传协议 capability。"""
        return self._protocol.acquisition_mode

    # ------------------------------------------------------------------
    # 点表
    # ------------------------------------------------------------------

    def set_points(self, points: list[PointConfig]) -> None:
        """更新点表并重注入协议映射（热重载轻量路径，不触碰连接）。"""
        self._points = points
        self._protocol.set_points_mapping(points)

    def point_group_points(self, point_group: str) -> list[PointConfig]:
        """选出 ``point_groups`` 含 ``point_group`` 的全部点位。"""
        return [p for p in self._points if point_group in p.point_groups]

    def point_refs(self, point_group: str) -> list[PointRef]:
        """该 point_group 的批量读寻址引用。"""
        return [
            PointRef(device_id=self.device_id, point_id=p.point_id)
            for p in self.point_group_points(point_group)
        ]

    # ------------------------------------------------------------------
    # 点值解释（scale/offset——采集链路中唯一的值变换）
    # ------------------------------------------------------------------

    def _normalize_values(self, values: list[PointValue]) -> list[PointValue]:
        """把协议原生值按点表换算为工程值：``value = raw * scale + offset``。

        仅对数值（``int`` / ``float``，``bool`` 除外）值执行；``None`` /
        ``str`` / ``bool`` 原样透传。``quality`` / ``timestamp`` / ``source``
        等元数据一律不变——quality 只来自协议原生判定。点表查不到的点
        （防御性分支，正常不会发生）原样透传。
        """
        by_id = {p.point_id: p for p in self._points}
        out: list[PointValue] = []
        for pv in values:
            point = by_id.get(pv.point_id)
            if (
                point is None
                or (point.scale == 1.0 and point.offset == 0.0)
                or not isinstance(pv.value, int | float)
                or isinstance(pv.value, bool)
            ):
                out.append(pv)
                continue
            out.append(pv.model_copy(update={"value": pv.value * point.scale + point.offset}))
        return out

    # ------------------------------------------------------------------
    # 连接生命周期（委托协议实例）
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self._protocol.connect()

    async def close(self) -> None:
        await self._protocol.close()

    def health(self) -> HealthStatus:
        return self._protocol.health()

    # ------------------------------------------------------------------
    # 读写（委托协议实例）
    # ------------------------------------------------------------------

    async def read(self, point_group: str) -> list[PointValue]:
        """批量读取该 point_group 的全部点位，返回工程值（scale/offset 已应用）。"""
        return self._normalize_values(await self._protocol.read(self.point_refs(point_group)))

    async def read_points(self, refs: list[PointRef]) -> list[PointValue]:
        """按显式引用批量读取（CLI/API 单次读取路径），返回工程值。"""
        return self._normalize_values(await self._protocol.read(refs))

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """批量写命令——每个命令对应一个 :class:`CommandResult`。"""
        return await self._protocol.write(cmds)

    # ------------------------------------------------------------------
    # 持续采集
    # ------------------------------------------------------------------

    async def start_acquisition(
        self,
        *,
        point_group: str,
        interval: float | None,
        acquire: Callable[[], Awaitable[None]] | None = None,
        on_data: Callable[[list[PointValue]], Awaitable[None]] | None = None,
        on_poll_stats: Callable[[float, bool, int], None] | None = None,
    ) -> AcquisitionHandle:
        """启动本设备上一个 Task Instance 的持续采集，返回可关闭句柄。

        采集机制由协议 capability 决定，调用方不感知：

        - ``POLL``：``interval`` 必填且 > 0；以 fixed-rate 调度调用
          ``acquire``（由 Runtime 注入「读 → 处理 → 派发」闭包）；
        - ``SUBSCRIBE``：注册协议订阅，数据到达即 ``on_data(batch)``；
          ``interval`` 透传给协议（ADS 作为 notification cycle_time；
          IEC104 忽略）。协议实现 ``InterrogationCapable`` 时订阅建立
          后自动发送一次 General Interrogation。

        Raises:
            ConfigError: 采集机制与参数不匹配（POLL 缺 interval、ADS
                ``sequential`` 设备不允许持续采集等）。
        """
        if self._protocol.acquisition_mode is AcquisitionMode.POLL:
            if not self._config.supports_scheduled_collection:
                raise ConfigError(
                    f"Device '{self.device_id}' does not support scheduled collection "
                    "(ADS read_mode='sequential' is single-read only)"
                )
            if interval is None or interval <= 0:
                raise ConfigError(
                    f"Device '{self.device_id}' polls actively — "
                    f"interval must be configured and > 0, got {interval}"
                )
            if acquire is None:
                raise ConfigError(
                    f"Device '{self.device_id}': POLL acquisition requires an acquire callback"
                )
            handle = PollingAcquisitionHandle(interval, acquire, on_stats=on_poll_stats)
            await handle.start()
            return handle

        if on_data is None:
            raise ConfigError(
                f"Device '{self.device_id}': SUBSCRIBE acquisition requires an on_data callback"
            )

        async def _forward(value: PointValue) -> None:
            # 订阅上送没有 PointRef 上下文，协议驱动无法保证盖上设备身份
            # （IEC104 上送 device_id 为空）——采集句柄本就属于本设备，
            # 由 Device 聚合统一补盖；协议原生值换算为工程值（与轮询同语义）。
            stamped = value.model_copy(update={"device_id": self.device_id})
            await on_data(self._normalize_values([stamped]))

        subscription = await self._protocol.subscribe(
            self.point_refs(point_group), _forward, interval=interval
        )
        if isinstance(self._protocol, InterrogationCapable):
            # 先建立订阅、再发总召——总召响应经既有订阅链路上报。
            await self._protocol.interrogate()
        return _SubscriptionAcquisitionHandle(subscription)
