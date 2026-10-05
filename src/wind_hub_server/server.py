"""wind-hub-server process host.

Server owns Web/Admin/Config/Quality concerns；Collector/Commander 作为独立
Worker 仅通过 gRPC 访问。
"""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import clear_context, set_context
from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.task.reconcile import TaskPlacementReconciler
from wind_hub_server.application.worker.registry import WorkerRegistry
from wind_hub_server.assembly import ServerApp, assemble_server
from wind_hub_server.settings import ServerSettings

logger = logging.getLogger(__name__)


def build_api_server(
    runtime: ServerApp,
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
        len(runtime.config.current_config.devices.devices),
        len(runtime.config.current_config.sinks.sinks),
        settings.host,
        settings.port,
    )
    return uvicorn.Server(config)


async def reload_once(config: ConfigService) -> None:
    """Execute one incremental config reload."""
    logger.info("收到 SIGHUP，开始热重载")
    result = await config.reload()
    if result.success:
        logger.info("配置热重载成功")
    else:
        logger.warning("配置热重载失败：%s", result.errors)


async def _reload_loop(
    reload_event: asyncio.Event,
    config: ConfigService,
) -> None:
    while True:
        await reload_event.wait()
        reload_event.clear()
        await reload_once(config)


async def _worker_probe_loop(
    registry: WorkerRegistry,
    *,
    interval: float,
) -> None:
    """周期刷新 Worker Registry；状态变化才记录日志。"""
    previous: dict[str, str] = {}
    while True:
        try:
            records = await registry.refresh()
            current = {record.worker_id: record.state.value for record in records}
            if current != previous:
                logger.info("worker registry state: %s", current)
                previous = current
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("worker registry refresh failed", exc_info=True)
        await asyncio.sleep(interval)


async def _task_placement_reconcile_loop(
    reconciler: TaskPlacementReconciler,
    *,
    interval: float,
) -> None:
    """周期收敛 Task placement，阻止错误 Worker 持续采集。"""
    previous: tuple[
        bool,
        int,
        int,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ] | None = None
    while True:
        try:
            result = await reconciler.reconcile()
            current = (
                result.safe,
                result.wrong_running_instances,
                result.stopped_instances,
                tuple(result.unavailable_workers),
                tuple(result.orphaned_tasks),
                tuple(result.errors),
            )
            if current != previous:
                log = (
                    logger.warning
                    if not result.safe or result.wrong_running_instances
                    else logger.info
                )
                log(
                    "task placement reconciliation: safe=%s generation=%d "
                    "workers=%d examined=%d wrong_running=%d stopped=%d "
                    "unavailable=%s orphaned=%s errors=%s",
                    result.safe,
                    result.generation,
                    result.scanned_workers,
                    result.examined_instances,
                    result.wrong_running_instances,
                    result.stopped_instances,
                    result.unavailable_workers,
                    result.orphaned_tasks,
                    result.errors,
                )
                previous = current
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("task placement reconciliation failed", exc_info=True)
        await asyncio.sleep(interval)


async def _reconcile_loop(
    config: ConfigService,
    *,
    interval: float,
) -> None:
    """周期检查 Worker revision/hash，仅在结果状态变化时记录日志。"""
    previous_noteworthy: dict[str, str] = {}
    while True:
        try:
            outcomes = await config.reconcile_workers()
            noteworthy = {
                name: outcome
                for name, outcome in outcomes.items()
                if outcome not in {"already-current", "transactions-closed"}
            }
            if noteworthy != previous_noteworthy:
                if noteworthy:
                    has_error = any(
                        outcome.startswith(
                            (
                                "status-error:",
                                "prepare-error:",
                                "prepare-failed:",
                                "prepare-hash-mismatch:",
                                "activate-failed:",
                                "activate-unknown:",
                                "activate-hash-mismatch:",
                            )
                        )
                        for outcome in noteworthy.values()
                    )
                    log = logger.warning if has_error else logger.info
                    log("worker config reconciliation: %s", noteworthy)
                elif previous_noteworthy:
                    logger.info("worker config reconciliation recovered")
                previous_noteworthy = noteworthy
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("worker config reconciliation failed", exc_info=True)

        await asyncio.sleep(interval)


async def _wait_for_shutdown_or_api_exit(
    shutdown_event: asyncio.Event,
    api_task: asyncio.Task[None],
) -> bool:
    """等待停机信号或 API task 提前退出。

    Returns:
        API task 先结束时返回 True；收到正常停机信号时返回 False。

    Raises:
        Exception: API task 异常退出时原样传播，触发进程级失败与统一清理。
    """
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait(
            {shutdown_task, api_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if api_task in done:
            await api_task
            return True
        return False
    finally:
        if not shutdown_task.done():
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, return_exceptions=True)


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

    runtime = assemble_server(
        settings.config_dir,
        collectors=settings.collector_endpoints,
        commander=settings.commander,
    )
    runtime.config.initialize_desired_revision()

    runtime.log_store.install()
    set_context(runtime.context)

    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()
    _install_signal_handlers(shutdown_event, reload_event)

    server = build_api_server(runtime, settings)
    api_task: asyncio.Task[None] | None = None
    reload_task: asyncio.Task[None] | None = None
    reconcile_task: asyncio.Task[None] | None = None
    placement_reconcile_task: asyncio.Task[None] | None = None
    worker_probe_task: asyncio.Task[None] | None = None

    try:
        await runtime.worker_registry.refresh()
        placement = await runtime.task_reconciler.reconcile()
        if placement.wrong_running_instances:
            logger.warning(
                "task placement startup reconciliation stopped %d/%d "
                "wrong running instance(s)",
                placement.stopped_instances,
                placement.wrong_running_instances,
            )
        if not placement.safe:
            logger.warning(
                "task placement is not safe; start operations remain blocked: "
                "unavailable=%s orphaned=%s errors=%s",
                placement.unavailable_workers,
                placement.orphaned_tasks,
                placement.errors,
            )

        api_task = asyncio.create_task(server.serve())
        await runtime.monitoring.start()

        logger.info(
            "wind-hub-server 已启动（%d 台设备，%d 个 sink）；API %s:%d",
            len(runtime.config.current_config.devices.devices),
            len(runtime.config.current_config.sinks.sinks),
            settings.host,
            settings.port,
        )
        reload_task = asyncio.create_task(
            _reload_loop(reload_event, runtime.config)
        )
        reconcile_task = asyncio.create_task(
            _reconcile_loop(
                runtime.config,
                interval=settings.reconcile_interval,
            )
        )
        placement_reconcile_task = asyncio.create_task(
            _task_placement_reconcile_loop(
                runtime.task_reconciler,
                interval=settings.reconcile_interval,
            )
        )
        worker_probe_task = asyncio.create_task(
            _worker_probe_loop(
                runtime.worker_registry,
                interval=settings.worker_probe_interval,
            )
        )
        api_exited = await _wait_for_shutdown_or_api_exit(
            shutdown_event,
            api_task,
        )
        if api_exited:
            logger.warning("API server 已提前退出，开始进程级停机")
        else:
            logger.info("收到停机信号，开始优雅停机")

        runtime.config.stop_accepting_transactions()
        transaction_idle = await runtime.config.wait_for_transactions(
            timeout=settings.shutdown_timeout
        )
        if not transaction_idle:
            logger.warning(
                "配置事务在 %.1f 秒停机宽限期内未结束，将继续执行进程级停机",
                settings.shutdown_timeout,
            )
    finally:
        try:
            background_tasks = [
                task
                for task in (
                    reload_task,
                    reconcile_task,
                    placement_reconcile_task,
                    worker_probe_task,
                )
                if task is not None
            ]
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                await asyncio.gather(
                    *background_tasks,
                    return_exceptions=True,
                )

            server.should_exit = True
            if api_task is not None:
                if api_task.done():
                    await asyncio.gather(api_task, return_exceptions=True)
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(api_task),
                            timeout=settings.shutdown_timeout,
                        )
                    except TimeoutError:
                        logger.warning(
                            "API server 未在 %.1f 秒内退出，取消其任务",
                            settings.shutdown_timeout,
                        )
                        api_task.cancel()
                        await asyncio.gather(api_task, return_exceptions=True)

            await runtime.monitoring.stop()
        finally:
            await asyncio.gather(
                *(client.close() for client in runtime.collector_clients.values()),
                runtime.commander_client.close(),
                return_exceptions=True,
            )
            clear_context()
            runtime.log_store.uninstall()

    logger.info("wind-hub-server 已干净退出")
    return 0
