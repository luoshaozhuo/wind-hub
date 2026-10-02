"""IEC104 自发数据订阅注册表。

支持全局订阅和按 IOA 订阅。每个 callback 通过独立 asyncio task 调用，避免慢
callback 阻塞协议接收循环；callback 异常只记录日志，不反向破坏 session。
每次 subscribe 返回独立 handle，关闭 handle 只注销本调用方。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from wind_hub.domain.model.point import PointRef, PointValue

logger = logging.getLogger(__name__)


class IEC104Subscription:
    """一次订阅的句柄——close 只注销本次订阅，不影响同设备的其他订阅。

    实现 :class:`~wind_hub.domain.port.outbound.SubscriptionHandle`。
    """

    def __init__(
        self,
        registry: SubscriptionRegistry,
        callback: Callable[[PointValue], Awaitable[None]],
        ioas: list[int] | None,
    ) -> None:
        self._registry = registry
        self._callback = callback
        # ``None`` 表示全局订阅；否则为注册到的 IOA 列表。
        self._ioas = ioas
        self._closed = False

    async def close(self) -> None:
        """注销本次订阅（幂等）——只移除本句柄的回调，不触碰连接。"""
        if self._closed:
            return
        self._closed = True
        self._registry._unsubscribe(self._callback, self._ioas)


class SubscriptionRegistry:
    """管理 PointValue callback 注册。

    空 points 表示全局订阅；非空 points 经 ioa_resolver 转为按 IOA 注册。
    """

    def __init__(self) -> None:
        self._global: list[Callable[[PointValue], Awaitable[None]]] = []
        self._ioa: dict[int, list[Callable[[PointValue], Awaitable[None]]]] = {}

    # ==================================================================
    # 订阅注册
    # ==================================================================

    def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        ioa_resolver: Callable[[PointRef], int | None],
    ) -> IEC104Subscription:
        """注册订阅并返回独立句柄。

        Args:
            points: 订阅点；空列表表示接收全部 PointValue。
            callback: 点值异步回调。
            ioa_resolver: PointRef 到 IOA 的解析函数；未知点返回 None。

        Returns:
            只管理本次回调注册的 IEC104Subscription。
        """
        if not points:
            self._global.append(callback)
            logger.info(
                "IEC104: registered global subscription (total=%d)",
                len(self._global),
            )
            return IEC104Subscription(self, callback, None)

        ioas: list[int] = []
        for ref in points:
            ioa = ioa_resolver(ref)
            if ioa is None:
                logger.warning(
                    "IEC104: subscribe: unknown point '%s' — skipping",
                    ref.point_id,
                )
                continue
            self._ioa.setdefault(ioa, []).append(callback)
            ioas.append(ioa)
        logger.info(
            "IEC104: registered subscriptions for %d IOAs",
            len(points),
        )
        return IEC104Subscription(self, callback, ioas)

    def _unsubscribe(
        self,
        callback: Callable[[PointValue], Awaitable[None]],
        ioas: list[int] | None,
    ) -> None:
        """移除一个全局或按 IOA 的 callback 注册。"""
        if ioas is None:
            with contextlib.suppress(ValueError):
                self._global.remove(callback)
            return
        for ioa in ioas:
            callbacks = self._ioa.get(ioa)
            if callbacks is None:
                continue
            with contextlib.suppress(ValueError):
                callbacks.remove(callback)
            if not callbacks:
                del self._ioa[ioa]

    def clear(self) -> None:
        """清空全部订阅；用于 Driver 整体关闭。"""
        self._global.clear()
        self._ioa.clear()

    # ==================================================================
    # 数据分发
    # ==================================================================

    async def dispatch(self, pv: PointValue, ioa: int) -> None:
        """把 PointValue 分发给全部匹配订阅者。

        Args:
            pv: 待分发点值。
            ioa: 点对应 IOA。

        Notes:
            每个 callback 独立创建 task；异常由 _invoke_callback 捕获并记录，
            不传播到协议接收循环。
        """
        callbacks: list[Callable[[PointValue], Awaitable[None]]] = []

        # 先加入全局 callback。
        callbacks.extend(self._global)

        # 再加入当前 IOA 的 callback。
        callbacks.extend(self._ioa.get(ioa, []))

        if not callbacks:
            return

        for cb in callbacks:
            asyncio.ensure_future(self._invoke_callback(cb, pv, ioa))

    async def _invoke_callback(
        self,
        cb: Callable[[PointValue], Awaitable[None]],
        pv: PointValue,
        ioa: int,
    ) -> None:
        """调用单个 callback，并隔离其异常。

        捕获 Exception 是为了保护协议接收循环；失败已记录完整堆栈。
        """
        try:
            await cb(pv)
        except Exception:
            logger.exception(
                "IEC104: subscriber callback raised (IOA %d, point '%s')",
                ioa,
                pv.point_id,
            )

    # ==================================================================
    # 统计
    # ==================================================================

    @property
    def global_count(self) -> int:
        """返回全局订阅 callback 数。"""
        return len(self._global)

    @property
    def ioa_count(self) -> int:
        """返回按 IOA 注册的 callback 总数。"""
        return sum(len(v) for v in self._ioa.values())

    @property
    def unique_ioa_count(self) -> int:
        """返回至少有一个订阅者的 IOA 数。"""
        return len(self._ioa)
