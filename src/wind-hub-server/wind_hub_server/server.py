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
from wind_hub_server.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub_server.application.usecase.worker_registry import WorkerRegistryUseCase
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
        len(runtime.config.current_config.devices.devices),
        len(runtime.config.current_config.system.sinks),
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
    candidate = None
    added_devices: set[str] = set()
    if validator is not None:
        try:
            candidate = config.load_disk()
            diff = compute_diff(config.current_config, candidate)
            added_devices = set(diff.devices.added)
        except Exception:
            logger.warning("reload 配置预检查失败", exc_info=True)

    result = await config.reload()
    if result.success:
        logger.info("配置热重载成功")
        if validator is not None and candidate is not None and added_devices:
            try:
                summary = await validator.validate_added_devices(
                    candidate,
                    added_devices,
                )
                logger.info(
                    "reload active validation completed: devices=%d errors=%d",
                    len(summary.reports),
                    summary.error_count,
                )
            except Exception:
                logger.warning(
                    "reload 后新增设备主动验证失败",
                    exc_info=True,
                )
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


async def _worker_probe_loop(
    registry: WorkerRegistryUseCase,
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


async def _reconcile_loop(
    config: ConfigUseCase,
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

    startup_config = ConfigUseCase.load_directory(settings.config_dir)
    runtime = assemble_server(
        settings.config_dir,
        collector_targets=settings.collector_endpoints,
        commander_target=settings.commander_target,
    )
    validator = ServerConfigValidator(
        settings.config_dir,
        runtime.commander_client,
    )
    validation = await validator.validate_startup(startup_config)
    logger.info(
        "startup active validation completed: devices=%d errors=%d repaired_points=%d",
        len(validation.reports),
        validation.error_count,
        validation.repaired_points,
    )

    if validation.repaired_points:
        logger.warning(
            "启动验证已修复 %d 个 ADS 点地址，开始激活修复后的配置",
            validation.repaired_points,
        )
        activation = await runtime.config.reload()
        if not activation.success:
            raise RuntimeError(
                "failed to activate startup validation repairs: "
                + "; ".join(activation.errors)
            )
        startup_config = runtime.config.current_config
        logger.info(
            "启动验证修复已在 Collector/Commander 激活，changed_tables=%s",
            activation.diff.point_tables_changed,
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
    worker_probe_task: asyncio.Task[None] | None = None

    try:
        api_task = asyncio.create_task(server.serve())
        await runtime.monitoring.start()
        await runtime.worker_registry.refresh()

        logger.info(
            "wind-hub-server 已启动（%d 台设备，%d 个 sink）；API %s:%d",
            len(runtime.config.current_config.devices.devices),
            len(runtime.config.current_config.system.sinks),
            settings.host,
            settings.port,
        )
        reload_task = asyncio.create_task(
            _reload_loop(reload_event, runtime.config, validator)
        )
        reconcile_task = asyncio.create_task(
            _reconcile_loop(
                runtime.config,
                interval=settings.reconcile_interval,
            )
        )
        worker_probe_task = asyncio.create_task(
            _worker_probe_loop(
                runtime.worker_registry,
                interval=settings.worker_probe_interval,
            )
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，开始优雅停机")
        runtime.config.stop_accepting_transactions()
        transaction_idle = await runtime.config.wait_for_transactions(timeout=30.0)
        if not transaction_idle:
            logger.warning(
                "配置事务在 30 秒停机宽限期内未结束，将继续执行进程级停机"
            )
    finally:
        try:
            background_tasks = [
                task
                for task in (reload_task, reconcile_task, worker_probe_task)
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
                await api_task

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
