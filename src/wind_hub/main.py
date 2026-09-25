"""进程入口——装配、信号处理、启动与优雅停机。

提供异步编排函数 :func:`run_application` 与同步入口 :func:`main` /
:func:`cli_entry`，含 Web API 的真实启动（内嵌 uvicorn）、
SIGHUP 热重载与 ``/metrics``。

信号语义：SIGINT / SIGTERM 触发 ``shutdown_event``，随后先停 API、再调用
:func:`stop_runtime` 幂等停机；SIGHUP 触发一次配置热重载（不重启进程）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import uvicorn

from wind_hub.adapter.inbound.webapi.app import build_api
from wind_hub.application.app_context import AppContext, set_context
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime

logger = logging.getLogger(__name__)


def _build_api_server(rt: AssembledRuntime, host: str, port: int) -> uvicorn.Server:
    """构建内嵌 uvicorn :class:`Server`（尚未开始 serving）。

    真正的 ``serve()`` 由调用方以 ``asyncio.Task`` 启动，以便与引擎停机的
    信号等待共存于同一事件循环；API 通过 ``AppContext`` 读取服务，因此这里
    只负责监听地址与 FastAPI 应用的绑定。
    """
    app = build_api()
    config = uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on")
    logger.info(
        "Web API 启动中（%d 台设备，%d 个 sink）→ %s:%d",
        rt.runtime.device_count,
        rt.runtime.sink_count,
        host,
        port,
    )
    return uvicorn.Server(config)


async def _handle_sighup(config: ConfigUseCase) -> None:
    """SIGHUP 触发热重载——调用一次 ``config.reload()`` 并记录结果。"""
    logger.info("收到 SIGHUP，开始热重载")
    result = await config.reload()
    if result.success:
        logger.info("配置热重载成功")
    else:
        logger.warning("配置热重载失败：%s", result.errors)


async def _reload_loop(reload_event: asyncio.Event, config: ConfigUseCase) -> None:
    """长期运行的 SIGHUP 监听协程——每次事件触发执行一次热重载。

    连续多次 SIGHUP 只会折叠为事件置位，循环逐次消费；协程由
    :func:`run_application` 启动一次，并在停机时取消。
    """
    while True:
        await reload_event.wait()
        reload_event.clear()
        await _handle_sighup(config)


async def run_application(
    config_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    shutdown_timeout: float = 30.0,
) -> int:
    """装配并启动引擎与 Web API，阻塞直到收到 SIGINT/SIGTERM，随后优雅停机。

    Args:
        config_dir: 现场配置目录（<site>/，含 system/devices/tasks.yaml；公共定义在同级 common/）。
        host: Web API 监听地址。
        port: Web API 监听端口。
        shutdown_timeout: 优雅停机整体超时（秒），透传给
            :func:`stop_runtime`。

    Returns:
        进程退出码：0 表示正常停机。
    """
    logging.basicConfig(level=logging.INFO)

    rt = assemble(config_dir)

    # 组合根把全部服务注入进程级 AppContext。
    set_context(
        AppContext(
            config=rt.config,
            tasks=rt.tasks,
            runtime=rt.runtime,
            command=rt.command,
            query=rt.query,
        )
    )

    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGHUP, reload_event.set)

    # 决策 1：API 先于 Runtime 监听——设备连接超时（每台等满 connect_timeout）
    # 不再阻塞 /health 的可用性；Runtime 未就绪期间 /health 如实报告 down。
    server = _build_api_server(rt, host, port)
    api_task = await start_runtime(rt, api_server=server)
    logger.info(
        "wind-hub 引擎已启动（%d 台设备，%d 个 sink）；Web API 已监听 %s:%d",
        rt.runtime.device_count,
        rt.runtime.sink_count,
        host,
        port,
    )

    reload_task = asyncio.create_task(_reload_loop(reload_event, rt.config))

    try:
        await shutdown_event.wait()
        logger.info("收到停机信号，开始优雅停机")
    finally:
        reload_task.cancel()

        # 1. 先停 API（请求额度不再接收新连接）
        server.should_exit = True
        if api_task is not None:
            await api_task
        # 2. 再停引擎
        await stop_runtime(rt, timeout=shutdown_timeout)

    logger.info("wind-hub 已干净退出")
    return 0


def main() -> int:
    """``python -m wind_hub.main`` 的同步入口（argparse 参数解析）。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub.main",
        description="启动 wind-hub 引擎并前台阻塞，等待信号停机。",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="现场配置目录（<site>/，含 system/devices/tasks.yaml；公共定义在同级 common/）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API 监听地址")
    parser.add_argument("--port", type=int, default=8080, help="API 监听端口")
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=30.0,
        help="优雅停机超时（秒）",
    )
    args = parser.parse_args()
    return asyncio.run(
        run_application(
            args.config,
            host=args.host,
            port=args.port,
            shutdown_timeout=args.shutdown_timeout,
        )
    )


def cli_entry() -> int:
    """:func:`main` 的等价入口别名，供外层脚本调用。"""
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
