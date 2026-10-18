"""进程级同步命令线程池：``asyncio.to_thread`` 的默认 executor 安装点。

所有协议 Driver（ADS/IEC104/Modbus）的阻塞同步调用都经
``asyncio.to_thread`` 进入事件循环的默认 executor；Driver 不得自建
每设备线程池。每个需要执行协议同步命令的进程（Collector/Commander）
在引导阶段调用一次 :func:`configure_default_thread_pool`。

同一事件循环内重复调用是幂等的：参数一致时返回已安装的 executor，
不新建线程池、不替换旧池；参数冲突时抛 :class:`ConfigError`，避免
两个引导路径用不同容量静默覆盖。安装记录以事件循环为弱键，循环
（Runner）关闭并被回收后记录自动消失，不影响 pytest 逐测试循环隔离。

释放跟随事件循环既有生命周期：``asyncio.Runner.close()``（含
``asyncio.run``）会关闭默认 executor，无需 Driver 或进程入口额外管理。
配置变更为 restart-required，不做运行时热替换。
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from concurrent.futures import ThreadPoolExecutor

from core.application.errors import ConfigError

logger = logging.getLogger(__name__)

# 每个事件循环至多一次安装：loop -> (executor, 配置的 max_workers)。
# 弱键：循环 GC 后条目自动移除，测试里的短命循环不会残留线程池引用。
_installed: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[ThreadPoolExecutor, int]] = (
    weakref.WeakKeyDictionary()
)


def configure_default_thread_pool(max_workers: int) -> ThreadPoolExecutor:
    """把当前事件循环的默认 executor 安装为进程级协议命令线程池。

    必须在事件循环运行时、任何协议命令之前调用。同一循环重复调用且
    ``max_workers`` 一致时幂等返回已安装的 executor；配置值与首次安装
    冲突时抛 :class:`ConfigError`（配置变更属 restart-required，不做
    热替换）。

    Args:
        max_workers: 线程池容量（正整数，合法范围由配置层校验）。

    Returns:
        该事件循环上安装的唯一共享 ``ThreadPoolExecutor``。
    """
    loop = asyncio.get_running_loop()
    existing = _installed.get(loop)
    if existing is not None:
        executor, installed_workers = existing
        if installed_workers != max_workers:
            raise ConfigError(
                f"default thread pool already configured with max_workers="
                f"{installed_workers}; refusing conflicting reconfiguration "
                f"with max_workers={max_workers} (restart required)"
            )
        return executor

    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="wind-hub-cmd",
    )
    loop.set_default_executor(executor)
    _installed[loop] = (executor, max_workers)
    logger.info(
        "进程级协议命令线程池已安装 max_workers=%d（restart-required）",
        max_workers,
    )
    return executor
