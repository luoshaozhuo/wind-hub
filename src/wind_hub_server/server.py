"""wind-hub-server 生命周期宿主。

架构位置：最外层进程宿主。负责把 ``wind_hub`` 的组合根、AppContext、
FastAPI/uvicorn 和操作系统信号接成一个可运行服务。

本模块不负责业务规则、设备协议、Task 调度、配置解析或 API 路由实现；
这些能力继续位于 ``wind_hub`` 的 application/domain/adapter/config 层。

生命周期语义：
- SIGINT/SIGTERM 触发优雅停机；
- SIGHUP 触发增量配置热重载；
- API 先于 Runtime 启动，避免设备连接超时阻塞健康检查；
- 停机时先停止接收 API 请求，再停止 Runtime；
- AppContext 在进程退出时清理，避免测试或嵌入式调用残留全局状态。
"""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from wind_hub.adapter.inbound.webapi.app import build_api
from wind_hub.application.app_context import AppContext, clear_context, set_context
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub_server.settings import ServerSettings

logger = logging.getLogger(__name__)


def build_api_server(rt: AssembledRuntime, settings: ServerSettings) -> uvicorn.Server:
    """构建尚未开始监听的内嵌 uvicorn Server。

    Args:
        rt: 已完成装配的 wind-hub Runtime 对象图。
        settings: 服务宿主启动参数。

    Returns:
        尚未调用 ``serve()`` 的 uvicorn Server。
    """
    app = build_api()
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        lifespan="on",
    )
    logger.info(
        "wind-hub-server API 启动中（%d 台设备，%d 个 sink）→ %s:%d",
        rt.runtime.device_count,
        rt.runtime.sink_count,
        settings.host,
        settings.port,
    )
    return uvicorn.Server(config)


async def reload_once(config: ConfigUseCase) -> None:
    """执行一次配置热重载并记录结果。

    Args:
        config: 当前进程装配的配置 Use Case。
    """
    logger.info("收到 SIGHUP，开始热重载")
    result = await config.reload()
    if result.success:
        logger.info("配置热重载成功")
    else:
        logger.warning("配置热重载失败：%s", result.errors)


async def _reload_loop(reload_event: asyncio.Event, config: ConfigUseCase) -> None:
    """长期消费 SIGHUP 事件，直到任务被进程停机流程取消。"""
    while True:
        await reload_event.wait()
        reload_event.clear()
        await reload_once(config)


def _install_signal_handlers(
    shutdown_event: asyncio.Event,
    reload_event: asyncio.Event,
) -> None:
    """把 Unix 进程信号映射到 asyncio Event。

    ``wind-hub-server`` 的目标生产环境是 Linux/openEuler，因此使用事件循环
    signal handler；不在这里加入 Windows 专用降级路径，避免掩盖部署环境差异。

    Args:
        shutdown_event: SIGINT/SIGTERM 的停机事件。
        reload_event: SIGHUP 的热重载事件。
    """
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGHUP, reload_event.set)


def _set_context(rt: AssembledRuntime) -> None:
    """把一次装配得到的 Use Case/Runtime 注入 Web API 共享上下文。"""
    set_context(
        AppContext(
            config=rt.config,
            tasks=rt.tasks,
            runtime=rt.runtime,
            command=rt.command,
            query=rt.query,
            devices=rt.devices,
            overview=rt.overview,
            operations=rt.operations,
        )
    )


async def run_server(settings: ServerSettings) -> int:
    """启动 wind-hub-server 并阻塞到收到停机信号。

    Args:
        settings: 进程启动参数。业务配置仍从 ``settings.config_dir`` 指向
            的 YAML 配置集加载。

    Returns:
        进程退出码；正常优雅停机返回 0。

    Raises:
        ConfigError: 配置装载或对象图装配失败时由 ``assemble`` 透传。
        TimeoutError: Runtime 在 ``shutdown_timeout`` 内无法完成停机时透传。

    Notes:
        本函数拥有本进程 AppContext 和 reload task 的生命周期。设备连接、
        Sink 打开、采集任务启动和具体停机步骤由 Runtime/assembly 负责。
    """
    logging.basicConfig(level=logging.INFO)

    rt = assemble(settings.config_dir)
    _set_context(rt)

    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()
    _install_signal_handlers(shutdown_event, reload_event)

    server = build_api_server(rt, settings)
    api_task: asyncio.Task[None] | None = None
    reload_task: asyncio.Task[None] | None = None

    try:
        # API 监听先行；真正的 Runtime 启动由 assembly 保持单一实现。
        api_task = await start_runtime(rt, api_server=server)
        logger.info(
            "wind-hub-server 已启动（%d 台设备，%d 个 sink）；API %s:%d",
            rt.runtime.device_count,
            rt.runtime.sink_count,
            settings.host,
            settings.port,
        )
        reload_task = asyncio.create_task(_reload_loop(reload_event, rt.config))
        await shutdown_event.wait()
        logger.info("收到停机信号，开始优雅停机")
    finally:
        try:
            if reload_task is not None:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

            # 先拒绝新 API 请求，再释放协议、采集实例与 Sink 资源。
            server.should_exit = True
            if api_task is not None:
                await api_task
            await stop_runtime(rt, timeout=settings.shutdown_timeout)
        finally:
            # 即使停机超时/失败，也不能把上一轮进程上下文泄漏给嵌入式测试。
            clear_context()

    logger.info("wind-hub-server 已干净退出")
    return 0
