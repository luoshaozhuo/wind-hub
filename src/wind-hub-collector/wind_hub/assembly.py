"""Collector 组合根——只装配采集、控制与交付核心。

本模块属于 wind-hub-collector。它只构建 wind-hub-ctl / gRPC 控制面实际
需要的对象图：

Protocol / Device / Sink -> AcquisitionEngine -> Runtime
                           -> TaskUseCase / CommandUseCase / QueryUseCase / ConfigUseCase

Admin、Overview、Quality、Diagnostics、Web API、长期日志与 System Health
均不属于 Collector 组合根；运行时配置 reload 属于 Collector 核心控制面。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import wind_hub.adapter.outbound.protocol  # noqa: F401
from wind_hub.adapter.inbound.iec104_slave import (
    DataSnapshot,
    IEC104SlaveHandlers,
    IEC104SlaveServer,
    SlaveBridge,
    build_data_type_mapping,
    build_ioa_mapping,
    build_reverse_mapping,
)
from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub.application.port.sink import SinkPort
from wind_hub.application.runtime import Device, Runtime
from wind_hub.application.usecase.command import CommandUseCase
from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub.application.usecase.task import TaskUseCase
from wind_hub.config.loader import load_config
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    ReportingConfig,
    SinkConfig,
)
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.port.outbound import ProtocolPort
from wind_hub.infra.protocol_registry import protocol_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AssembledRuntime:
    """Collector 最小运行对象图。"""

    boot_config: Config
    runtime: Runtime
    engine: AcquisitionEngine
    dispatcher: CommandDispatcher
    sinks: dict[str, SinkPort]
    tasks: TaskUseCase
    command: CommandUseCase
    query: QueryUseCase
    config: ConfigUseCase
    iec104_slave: IEC104SlaveServer | None = None


def assemble(
    config_dir: str | Path,
    sink_factory: Callable[[SinkConfig], SinkPort] | None = None,
) -> AssembledRuntime:
    """从 YAML 配置同步装配 Collector，不执行网络 I/O。"""
    cfg = load_config(config_dir)
    make_sink = sink_factory or _create_sink

    devices: dict[str, Device] = {}
    for device_cfg in cfg.devices.devices:
        protocol = _create_protocol(device_cfg)
        points = cfg.points_for_device(device_cfg.device_id)
        protocol.set_points_mapping(points)
        devices[device_cfg.device_id] = Device(
            config=device_cfg,
            points=points,
            protocol=protocol,
        )

    sinks = {sink.name: make_sink(sink) for sink in cfg.system.sinks}

    dispatcher = CommandDispatcher(
        devices,
        default_timeout=cfg.system.runtime.write_timeout,
    )

    engine = AcquisitionEngine(
        read_timeout=cfg.system.runtime.read_timeout,
    )

    runtime = Runtime(
        devices=devices,
        sinks=sinks,
        engine=engine,
        dispatcher=dispatcher,
        config=cfg.system.runtime,
        tasks={task.task_id: task for task in cfg.tasks.tasks},
        protocol_factory=_create_protocol,
        sink_factory=make_sink,
    )

    tasks = TaskUseCase(runtime)
    command = CommandUseCase(dispatcher)
    query = QueryUseCase(runtime)
    config = ConfigUseCase(config_dir, runtime, cfg)

    iec104_slave: IEC104SlaveServer | None = None
    if cfg.reporting is not None and cfg.reporting.reporting:
        iec104_slave = _build_iec104_slave(
            cfg.reporting,
            engine,
            dispatcher,
        )

    return AssembledRuntime(
        boot_config=cfg,
        runtime=runtime,
        engine=engine,
        dispatcher=dispatcher,
        sinks=sinks,
        tasks=tasks,
        command=command,
        query=query,
        config=config,
        iec104_slave=iec104_slave,
    )


async def start_runtime(rt: AssembledRuntime) -> None:
    """启动 Collector Runtime 及可选 IEC104 reporting 从站。"""
    await _maybe_init_ads_local(rt)
    await rt.runtime.start()

    if rt.iec104_slave is not None:
        try:
            await rt.iec104_slave.start()
            logger.info("IEC104 从站代理已启动")
        except Exception:
            logger.warning(
                "IEC104 从站代理启动失败——Collector 继续运行",
                exc_info=True,
            )


async def _maybe_init_ads_local(rt: AssembledRuntime) -> None:
    """存在 ADS 设备时执行一次进程级本机 AMS 初始化。"""
    ads_cfg = rt.boot_config.system.ads
    if ads_cfg is None:
        return
    if not any(
        device.protocol == "ads"
        for device in rt.boot_config.devices.devices
    ):
        return

    from wind_hub.adapter.outbound.protocol.ads import router as ads_router

    try:
        await ads_router.ensure_local_initialized(ads_cfg)
    except Exception:
        logger.warning(
            "ADS 本机初始化失败——ADS 设备连接将继续尝试",
            exc_info=True,
        )


async def stop_runtime(
    rt: AssembledRuntime,
    timeout: float = 30.0,
) -> None:
    """停止可选 IEC104 从站并优雅停止 Collector Runtime。"""
    if rt.iec104_slave is not None:
        try:
            await rt.iec104_slave.stop()
        except Exception:
            logger.warning("IEC104 从站代理停止失败", exc_info=True)

    await asyncio.wait_for(rt.runtime.stop(), timeout=timeout)


def _create_protocol(cfg: DeviceConfig) -> ProtocolPort:
    """按协议注册表创建设备驱动。"""
    return protocol_registry.create(cfg.protocol, cfg)


def _build_iec104_slave(
    reporting: ReportingConfig,
    engine: AcquisitionEngine,
    dispatcher: CommandDispatcher,
) -> IEC104SlaveServer:
    """装配可选 IEC104 reporting 从站代理。"""
    snapshot = DataSnapshot()
    ioa_mapping = build_ioa_mapping(reporting.reporting)
    data_type_mapping = build_data_type_mapping(reporting.reporting)
    reverse_mapping = build_reverse_mapping(reporting.reporting)

    bridge = SlaveBridge(
        dispatcher,
        snapshot,
        ioa_mapping,
    )
    engine.add_observer(bridge.on_points_collected)

    handlers = IEC104SlaveHandlers(
        snapshot=snapshot,
        data_type_mapping=data_type_mapping,
        reverse_mapping=reverse_mapping,
        bridge=bridge,
        common_address=reporting.common_address,
        batch_size=reporting.batch_size,
    )
    return IEC104SlaveServer(
        host=reporting.host,
        port=reporting.port,
        handlers=handlers,
        common_address=reporting.common_address,
    )


def _create_sink(cfg: SinkConfig) -> SinkPort:
    """按配置类型懒加载并创建 Collector Sink。

    可选 Sink 依赖只在配置实际使用该类型时导入，File-only 部署无需安装
    aiokafka/asyncpg。
    """
    if cfg.type == "file":
        from wind_hub.adapter.outbound.sink.file.csv import FileSink

        return FileSink(cfg)
    if cfg.type == "kafka":
        from wind_hub.adapter.outbound.sink.mq.kafka import KafkaSink

        return KafkaSink(cfg)
    if cfg.type == "db":
        from wind_hub.adapter.outbound.sink.db.postgres import DBSink

        return DBSink(cfg)
    raise ConfigError(
        f"Unknown sink type '{cfg.type}' (available: kafka, file, db)"
    )
