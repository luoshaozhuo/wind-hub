"""每个事件循环的 ADS 路由准入与 AMS 端口保护。"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from core.application.errors import ProtocolError

if TYPE_CHECKING:
    from .driver import ADSDriver
    from .session import _AdsSession


class _RouteState:
    """单个 AdsLib 目标路由（目标 Net ID；缺省退化为主机地址）的循环级状态。

    pyads 3.6.0 在 Linux 上 ``Connection.open()`` 先开连接私有端口再
    ``adsAddRoute(目标 Net ID, ip)``，``Connection.close()`` 则**先**
    ``adsDelRoute(目标 Net ID)`` 再关私有端口。AdsLib 路由按目标 Net ID
    进程级共享、不按 Connection 引用计数：同 Net ID 的多个活动 Connection
    中任意一个 close 都会无条件删掉其他连接仍在依赖的路由。因此：

    - ``op_lock`` 串行化同一路由的「建链段」与「物理关闭段」：新连接的
      adsAddRoute 不得与旧连接迟到的 adsDelRoute 交错（I1/I3）。锁在
      Driver close 流程中跨越异步预算持有，物理关闭线程迟到收敛后才释放，
      因此不需要单独的"未收敛"等待队列；
    - ``active`` 登记已成功建链的 Driver，从建链成功保留到物理关闭收敛，
      使 close_local() 在「已摘除但物理关闭未完成」窗口内也能看到占用；
    - ``closing`` 登记物理关闭已提交（含建链失败的当场清理）的 Session，
      登记先于底层 close() 启动（I2），线程真实结束后摘除。

    不同路由键持有独立的 ``_RouteState``，单设备关闭故障不会阻塞其他
    Net ID 设备的建链与采集（I6）。

    状态按事件循环持有：生产进程只有一个循环；测试每个用例使用独立循环，
    3.11 的 contended asyncio.Lock 会校验循环归属，必须随循环隔离。
    """

    __slots__ = ("key", "op_lock", "active", "closing", "unsafe_reason")

    def __init__(self, key: str) -> None:
        self.key = key
        self.op_lock = asyncio.Lock()
        self.active: set[ADSDriver] = set()
        self.closing: set[_AdsSession] = set()
        self.unsafe_reason: str | None = None


class _LoopRouteRegistry:
    """单个事件循环内的路由协调状态与进程级 AMS 登记闸门。"""

    __slots__ = ("routes", "barrier", "local_closed")

    def __init__(self) -> None:
        self.routes: dict[str, _RouteState] = {}
        # 建链/关链登记闸门：所有改变 AdsLib 路由或 AMS 端口进程状态的
        # 「登记瞬间」与 close_local() 的「检查 + 拆除」经它互斥。只包裹
        # 极短的登记/拆除段，不包裹物理 close 线程（避免单设备故障全局
        # 串行化，见 I6）。
        self.barrier = asyncio.Lock()
        # close_local() 拆除进程端口后闩锁为真，此后新的建链一律快速失败，
        # 直到再次 initialize_local() 复位。跟随事件循环隔离（见类文档）。
        self.local_closed = False


_REGISTRIES_LOCK = threading.Lock()
# asyncio 事件循环支持弱引用：循环结束即自动丢弃其协调状态。
_REGISTRIES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopRouteRegistry] = (
    weakref.WeakKeyDictionary()
)


def _registry() -> _LoopRouteRegistry:
    """取得当前事件循环的路由协调注册表（惰性创建）。"""
    loop = asyncio.get_running_loop()
    with _REGISTRIES_LOCK:
        registry = _REGISTRIES.get(loop)
        if registry is None:
            registry = _LoopRouteRegistry()
            _REGISTRIES[loop] = registry
        return registry


def _route_state(key: str) -> _RouteState:
    """取得（惰性创建）当前循环内路由键对应的协调状态。"""
    registry = _registry()
    state = registry.routes.get(key)
    if state is None:
        state = _RouteState(key)
        registry.routes[key] = state
    return state


@contextlib.asynccontextmanager
async def _ams_barrier() -> AsyncIterator[None]:
    """进程级 AMS 登记闸门：建链准入检查/登记与 close_local 拆除互斥。"""
    registry = _registry()
    async with registry.barrier:
        if registry.local_closed:
            raise ProtocolError(
                "ADS local AMS port has been closed by close_local(); "
                "initialize_local() is required before new connections"
            )
        yield


@contextlib.asynccontextmanager
async def _ams_barrier_closing() -> AsyncIterator[None]:
    """物理关闭登记段的同一闸门；关闭清理在 close_local 之后仍允许执行。"""
    async with _registry().barrier:
        yield
