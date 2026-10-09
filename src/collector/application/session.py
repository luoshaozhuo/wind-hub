"""Collector 设备会话与持续采集生命周期。

CollectorDeviceSession 把共享领域对象（Device / PointTable）与
``core.application.ProtocolPort`` 组合成单台设备的采集边界：

- 读：按 point_group 选点批量读，``ProtocolSample`` 原始值按点表
  scale/offset 换算为工程值并盖上设备身份；
- 持续采集：fixed-rate polling（POLL）或协议订阅（SUBSCRIBE），
  机制由协议能力与进程级 ``subscribe_enabled`` 策略决定；
- 轻量热更新：点表/分组变化时就地换表（不重建协议连接）。

会话不主动建立连接；连接生命周期由 DeviceRuntime 管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from core.application import (
    ConfigError,
    ConnectionHealth,
    ProtocolCapability,
    ProtocolPort,
    ProtocolSample,
)
from core.domain import Device, DeviceId, Point, PointTable

from ..domain.point_value import PointValue
from .config import PointMeta

logger = logging.getLogger(__name__)


class AcquisitionMode(Enum):
    """设备持续采集机制。"""

    POLL = "poll"
    """主动轮询（Modbus、ADS Sum read）。"""

    SUBSCRIBE = "subscribe"
    """订阅推送（ADS notification、IEC104 spontaneous/periodic）。"""


class AcquisitionHandle(Protocol):
    """一次持续采集的句柄——关闭即停止采集，不影响同设备的其他实例。

    底层可能是 fixed-rate polling 协程、ADS notification 或 IEC104
    订阅；Runtime 只依赖 ``close()``，不感知具体机制。
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


class CollectorDeviceSession:
    """Collector 运行时设备会话。

    Args:
        device: 共享领域设备聚合。
        point_table: 设备型号绑定的 resolved 点表。
        point_meta: 本点表的进程级点位元数据（按 point_id 索引）。
        protocol: 组合根经 ProtocolRegistry 创建的协议 Driver。
        subscribe_enabled: ADS 设备是否使用订阅推送（notification）采集；
            非 ADS 设备忽略（IEC104 恒订阅、Modbus 恒轮询）。
        supports_scheduled_collection: 是否支持周期采集（ADS
            ``read_mode='sequential'`` 为单读模式，不支持）。
    """

    def __init__(
        self,
        device: Device,
        point_table: PointTable,
        point_meta: Mapping[str, PointMeta],
        protocol: ProtocolPort,
        *,
        subscribe_enabled: bool = False,
        supports_scheduled_collection: bool = True,
    ) -> None:
        self._device = device
        self._point_table = point_table
        self._point_meta = dict(point_meta)
        self._point_group_cache: dict[str, tuple[str, ...]] = {}
        self._protocol = protocol
        self._subscribe_enabled = subscribe_enabled
        self._supports_scheduled_collection = supports_scheduled_collection

    # ------------------------------------------------------------------
    # 身份与配置快照
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> DeviceId:
        """返回设备稳定标识。"""
        return self._device.device_id

    @property
    def device(self) -> Device:
        """返回当前设备领域快照。"""
        return self._device

    @property
    def device_group_ids(self) -> tuple[str, ...]:
        """返回设备所属分组（Task 按 device_group 展开用）。"""
        return tuple(str(g) for g in self._device.device_group_ids)

    @property
    def protocol_name(self) -> str:
        """返回设备点表协议名。"""
        return self._point_table.protocol.name

    @property
    def protocol(self) -> ProtocolPort:
        """返回当前协议运行实例。"""
        return self._protocol

    @property
    def point_table(self) -> PointTable:
        """返回当前设备绑定点表。"""
        return self._point_table

    @property
    def point_meta_map(self) -> Mapping[str, PointMeta]:
        """返回当前点表的进程级点位元数据（浅拷贝）。"""
        return dict(self._point_meta)

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """返回本设备的持续采集机制。

        - 协议声明 SUBSCRIBE 能力且（非 ADS 或进程级开启订阅）→ SUBSCRIBE；
        - 其余 → POLL（Modbus、ADS Sum read）。
        """
        if ProtocolCapability.SUBSCRIBE not in self._protocol.capabilities():
            return AcquisitionMode.POLL
        if self.protocol_name == "ads" and not self._subscribe_enabled:
            return AcquisitionMode.POLL
        return AcquisitionMode.SUBSCRIBE

    @property
    def supports_scheduled_collection(self) -> bool:
        """是否支持周期采集（ADS sequential 单读设备为 False）。"""
        return self._supports_scheduled_collection

    # ------------------------------------------------------------------
    # 轻量热更新（不重建协议连接）
    # ------------------------------------------------------------------

    def set_device(self, device: Device) -> None:
        """替换不要求重建协议实例的设备配置快照。"""
        self._device = device

    def set_points(self, point_table: PointTable, point_meta: Mapping[str, PointMeta]) -> None:
        """更新点表与点位元数据（只更新内存映射，不触碰协议连接）。

        点表绑定切换与表内容（地址/类型）变化共用本轻量路径：会话映射
        与协议 Driver 寻址一并更新，分组缓存与 Driver 读取缓存同步失效。
        订阅类设备的已注册通知由 TaskRuntime 重启订阅后按新映射重建。

        Driver 先行更新：Driver 侧校验失败时抛错，会话仍持旧表，不留下
        "会话新表 + Driver 旧映射" 的不一致状态。
        """
        self._protocol.update_point_table(point_table)
        self._point_table = point_table
        self._point_meta = dict(point_meta)
        self._point_group_cache.clear()

    # ------------------------------------------------------------------
    # 连接与健康（委托协议 Driver）
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """建立底层协议连接。"""
        await self._protocol.connect()

    async def close(self) -> None:
        """关闭底层协议连接并释放资源；重复调用由 Driver 保证安全。"""
        await self._protocol.close()

    def health(self) -> ConnectionHealth:
        """返回协议缓存的健康状态，不触发实时网络探测。"""
        return self._protocol.health()

    # ------------------------------------------------------------------
    # 选点与读取（实现 Domain ReadableDevice）
    # ------------------------------------------------------------------

    def point_ids(self, point_group: str) -> list[str]:
        """返回属于指定 point_group 的 point_id 列表（按点表顺序）。"""
        cached = self._point_group_cache.get(point_group)
        if cached is None:
            cached = tuple(point.point_id for point in self._group_points(point_group))
            self._point_group_cache[point_group] = cached
        return list(cached)

    async def read(self, point_group: str) -> list[PointValue]:
        """读取指定点组并返回已应用 scale/offset 的工程值（盖设备身份）。"""
        samples = await self._protocol.read_many(self.point_ids(point_group))
        return self._to_values(samples)

    def _group_points(self, point_group: str) -> list[Point]:
        """返回属于指定 point_group 的点定义。"""
        return [
            point
            for point in self._point_table.points.values()
            if point_group in self._meta(point.point_id).point_groups
        ]

    def _meta(self, point_id: str) -> PointMeta:
        """返回点位元数据；缺省时返回空元数据。"""
        return self._point_meta.get(point_id, PointMeta(variable_name=None, point_groups=()))

    def _to_values(
        self, samples: tuple[ProtocolSample, ...] | list[ProtocolSample]
    ) -> list[PointValue]:
        """把协议原始样本批量换算为工程值 PointValue（盖设备身份与协议来源）。"""
        return [self._to_value(sample) for sample in samples]

    def _to_value(self, sample: ProtocolSample) -> PointValue:
        """把单个协议原始样本换算为工程值 PointValue。

        只转换 int/float 且排除 bool；None、字符串和未知点原样保留，
        质量、时间戳不变。
        """
        point = self._point_table.points.get(sample.point_id)
        value = sample.value
        if (
            point is not None
            and not (point.scale == 1.0 and point.offset == 0.0)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            value = value * point.scale + point.offset
        return PointValue(
            device_id=str(self._device.device_id),
            point_id=sample.point_id,
            value=value,
            quality=sample.quality,
            # 协议未提供采样时间时回退到本地采集时刻（UTC）。
            timestamp=sample.timestamp or datetime.now(UTC),
            source=self.protocol_name,
        )

    # ------------------------------------------------------------------
    # 持续采集生命周期
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

        采集机制由 :attr:`acquisition_mode` 决定，调用方不感知：

        - ``POLL``：``interval`` 必填且 > 0；以 fixed-rate 调度调用
          ``acquire``（由 Runtime 注入「读 → 处理 → 派发」闭包）；
        - ``SUBSCRIBE``：注册协议订阅，数据到达即 ``on_data(batch)``；
          ``interval`` 透传给协议（ADS 作为 notification cycle_time；
          IEC104 忽略）。协议声明 INTERROGATE 能力时订阅建立后自动
          发送一次 General Interrogation。

        Raises:
            ConfigError: 采集机制与参数不匹配（POLL 缺 interval、ADS
                ``sequential`` 设备不允许持续采集等）。
        """
        if self.acquisition_mode is AcquisitionMode.POLL:
            if not self._supports_scheduled_collection:
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

        async def _forward(sample: ProtocolSample) -> None:
            # 订阅上送没有设备身份上下文，协议驱动无法保证盖上设备身份
            # （IEC104 上送 device_id 为空）——采集句柄本就属于本设备，
            # 由会话聚合统一补盖；协议原生值换算为工程值（与轮询同语义）。
            await on_data(self._to_values([sample]))

        subscription = await self._protocol.subscribe(
            self.point_ids(point_group), _forward, interval=interval
        )
        if ProtocolCapability.INTERROGATE in self._protocol.capabilities():
            # 先建立订阅、再发总召——总召响应经既有订阅链路上报。
            await self._protocol.interrogate()
        return subscription
