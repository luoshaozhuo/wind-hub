"""进程级同步命令线程池：``asyncio.to_thread`` 的默认 executor 安装点。

所有协议 Driver（ADS/IEC104/Modbus）的阻塞同步调用都经
``asyncio.to_thread`` 进入事件循环的默认 executor；Driver 不得自建
每设备线程池。每个需要执行协议同步命令的进程（Collector/Commander）
在引导阶段调用一次 :func:`configure_default_thread_pool`。

释放跟随事件循环既有生命周期：``asyncio.Runner.close()``（含
``asyncio.run``）会关闭默认 executor，无需 Driver 或进程入口额外管理。
配置变更为 restart-required，不做运行时热替换。
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def configure_default_thread_pool(max_workers: int) -> ThreadPoolExecutor:
    """把当前事件循环的默认 executor 安装为进程级协议命令线程池。

    必须在事件循环运行时、任何协议命令之前调用，且每进程只调用一次。
    Python 3.11 要求传入真实的 ``ThreadPoolExecutor`` 实例。
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="wind-hub-cmd",
    )
    loop.set_default_executor(executor)
    logger.info(
        "进程级协议命令线程池已安装 max_workers=%d（restart-required）",
        max_workers,
    )
    return executor
