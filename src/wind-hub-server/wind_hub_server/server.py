"""wind-hub-server process host.

Server owns Web/Admin/Config/Quality concerns.  Collector core remains isolated
behind ServerRuntime.collector during the current embedded-worker transition.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from wind_hub.assembly import start_runtime, stop_runtime
from wind_hub.config.loader import load_config
from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import clear_context, set_context
from wind_hub_server.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub_server.assembly import ServerRuntime, assemble_server
from wind_hub_server.config_validation import ServerConfigValidator
from wind_hub_server.settings import ServerSettings

logger = logging.getLogger(__name__)


def build_api_server(
    runtime: ServerRuntime,
    settings: ServerSettings,
) -> uvicorn.Server:
    """Build the Server-owned FastAPI/uvicorn host."""
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
        runtime.collector.runtime.device_count,
        runtime.collector.runtime.sink_count,
        settings.host,
        settings.port,
    )
    return uvicorn.Server(config)


async def reload_once(
    config: ConfigUseCase,
    validator: ServerConfigValidator | None = None,
) -> None:
    """Execute one incremental config reload."""
    logger.info("收到 SIGHUP，开始热重载")
    if validator is not None:
        try:
            candidate = load_config(config.config_dir)
            diff = compute_diff(config.current_config, candidate)
            summary = await validator.validate_added_devices(
                candidate,
                set(diff.devices.added),
            )
            if summary.reports:
                logger.info(
                    "reload active validation completed: devices=%d errors=%d",
                    len(summary.reports),
                    summary.error_count,
                )
        except Exception:
            logger.warning(
                "reload active validation failed before Runtime reload",
                exc_info=True,
            )

    result = await config.reload()
    if result.success:
        logger.info("配置热重载成功")
    else:
        logger.warning("配置热重载失败：%s", result.errors)


async def _reload_loop(
    reload_event: asyncio.Event,
    config: ConfigUseCase,
    validator: ServerConfigValidator,
) -> None:
    while True:
        await reload_event.wait()
        reload_event.clear()
        await reload_once(config, validator)


def _install_signal_handlers(
    shutdown_event: asyncio.Event,
    reload_event: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGHUP, reload_event.set)


async def run_server(settings: ServerSettings) -> int:
    """Run the Server-owned API and management composition."""
    logging.basicConfig(level=logging.INFO)

    validator = ServerConfigValidator(settings.config_dir)
    startup_config = load_config(settings.config_dir)
    validation = await validator.validate_startup(startup_config)
    logger.info(
        "startup active validation completed: devices=%d errors=%d repaired_points=%d",
        len(validation.reports),
        validation.error_count,
        validation.repaired_points,
    )

    runtime = assemble_server(settings.config_dir)
    runtime.log_store.install()
    set_context(runtime.context)

    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()
    _install_signal_handlers(shutdown_event, reload_event)

    server = build_api_server(runtime, settings)
    api_task: asyncio.Task[None] | None = None
    reload_task: asyncio.Task[None] | None = None

    try:
        # API listens first; device connection may take much longer.
        api_task = asyncio.create_task(server.serve())
        await start_runtime(runtime.collector)
        await runtime.monitoring.start()

        logger.info(
            "wind-hub-server 已启动（%d 台设备，%d 个 sink）；API %s:%d",
            runtime.collector.runtime.device_count,
            runtime.collector.runtime.sink_count,
            settings.host,
            settings.port,
        )
        reload_task = asyncio.create_task(
            _reload_loop(reload_event, runtime.config, validator)
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，开始优雅停机")
    finally:
        try:
            if reload_task is not None:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

            server.should_exit = True
            if api_task is not None:
                await api_task

            await runtime.monitoring.stop()
            await stop_runtime(
                runtime.collector,
                timeout=settings.shutdown_timeout,
            )
        finally:
            clear_context()
            runtime.log_store.uninstall()

    logger.info("wind-hub-server 已干净退出")
    return 0
