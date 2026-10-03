"""Collector 设备采集生命周期。

本模块只在 wind-hub-core DeviceSession 之上增加持续采集能力：fixed-rate polling、
协议订阅、总召触发以及采集句柄生命周期。连接、点表、工程值换算、即时读写和
health 均由 DeviceSession 提供。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from wind_hub_core.device.session import DeviceSession
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.port import (
    AcquisitionMode,
    InterrogationCapable,
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


class Device(DeviceSession):
    """Collector 运行时设备。

    基础连接、读写、点表和工程值换算继承自 DeviceSession；本类只增加周期轮询/
    协议订阅的采集生命周期，使 Collector Runtime 不承担设备通信细节。
    """

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
        if self.acquisition_mode is AcquisitionMode.POLL:
            if not self.config.supports_scheduled_collection:
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

        subscription = await self.protocol.subscribe(
            self.point_refs(point_group), _forward, interval=interval
        )
        if isinstance(self.protocol, InterrogationCapable):
            # 先建立订阅、再发总召——总召响应经既有订阅链路上报。
            await self.protocol.interrogate()
        return subscription
