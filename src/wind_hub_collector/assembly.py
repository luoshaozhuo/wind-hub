"""Collector 组合根——只装配采集、控制与交付核心。

本模块属于 wind-hub-collector。它只构建 wind-hub-ctl / gRPC 控制面实际
需要的对象图：

Protocol / Device / Sink -> AcquisitionEngine -> Runtime
                           -> TaskUseCase / CommandUseCase / QueryUseCase / ConfigUseCase

Admin、Overview、Quality、Web API、长期日志与 System Health 不属于 Collector
组合根；按需 Diagnostics 与运行时配置 reload 属于 Collector 核心控制面。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 导入模块以触发内置协议驱动注册；若注册机制改为显式装配，可删除该副作用导入与抑制。
import wind_hub_core.protocol  # noqa: F401
from wind_hub_collector.adapter.inbound.iec104_slave import (
    DataSnapshot,
    IEC104SlaveHandlers,
    IEC104SlaveServer,
    SlaveBridge,
    build_data_type_mapping,
    build_ioa_mapping,
    build_reverse_mapping,
)
from wind_hub_collector.application.command_dispatcher import CommandDispatcher
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import Device, Runtime
from wind_hub_collector.application.runtime.metrics_state import CollectorMetricsState
from wind_hub_collector.application.usecase.command import CommandUseCase
from wind_hub_collector.application.usecase.config import ConfigUseCase
from wind_hub_collector.application.usecase.diagnostic import DiagnosticUseCase
from wind_hub_collector.application.usecase.query import QueryUseCase
from wind_hub_collector.application.usecase.task import TaskUseCase
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import (
    Config,
    DeviceConfig,
    ReportingConfig,
    SinkConfig,
)
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.protocol.port import ProtocolPort
from wind_hub_core.protocol.registry import protocol_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AssembledRuntime:
    """Collector 最小运行对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Collector 运行时聚合根。
        engine: 采集执行引擎。
        dispatcher: 设备写指令分发器。
        sinks: 已装配的 Sink 实例注册表。
        tasks: Task / Task Instance 控制用例。
        command: 写指令用例。
        query: 只读查询用例。
        diagnostic: 按需现场诊断用例。
        config: 本地 YAML 增量热重载用例。
        iec104_slave: 可选 IEC104 reporting 从站代理。
    """

    boot_config: Config
    runtime: Runtime
    engine: AcquisitionEngine
    dispatcher: CommandDispatcher
    sinks: dict[str, SinkPort]
    tasks: TaskUseCase
    command: CommandUseCase
    query: QueryUseCase
    diagnostic: DiagnosticUseCase
    config: ConfigUseCase
    metrics_state: CollectorMetricsState
    iec104_slave: IEC104SlaveServer | None = None


def assemble(
    config_dir: str | Path,
    sink_factory: Callable[[SinkConfig], SinkPort] | None = None,
) -> AssembledRuntime:
    """从 YAML 配置同步装配 Collector，不执行网络 I/O。

    Args:
        config_dir: 现场配置目录。
        sink_factory: 可选 Sink 工厂；测试或定制部署可注入替代实现。

    Returns:
        完整但尚未启动的 AssembledRuntime。

    Raises:
        ConfigError: 配置缺失、跨文件引用非法或 Sink 类型未知。
        Exception: 协议/Sink 构造阶段的其他配置型异常原样传播。

    Notes:
        本函数只构造对象图和内存映射；设备连接、Sink open、IEC104 reporting
        监听均在 start_runtime 阶段发生。
    """
    cfg = load_config(config_dir)
    make_sink = sink_factory or _create_sink

    devices: dict[str, Device] = {}
    for device_cfg in cfg.devices.devices:
        protocol = _create_protocol(device_cfg)
        points = cfg.points_for_device(device_cfg.device_id)
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

    metrics_state = CollectorMetricsState()
    engine = AcquisitionEngine(
        read_timeout=cfg.system.runtime.read_timeout,
    )
    engine.add_observer(metrics_state.observe_points)

    runtime = Runtime(
        devices=devices,
        sinks=sinks,
        engine=engine,
        dispatcher=dispatcher,
        config=cfg.system.runtime,
        tasks={task.task_id: task for task in cfg.tasks.tasks},
        protocol_factory=_create_protocol,
        sink_factory=make_sink,
        metrics_hook=metrics_state,
    )

    tasks = TaskUseCase(runtime)
    command = CommandUseCase(dispatcher, runtime)
    query = QueryUseCase(runtime)
    diagnostic = DiagnosticUseCase(runtime)
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
        diagnostic=diagnostic,
        config=config,
        metrics_state=metrics_state,
        iec104_slave=iec104_slave,
    )


async def start_runtime(rt: AssembledRuntime) -> None:
    """启动 Collector Runtime 及可选 IEC104 reporting 从站。

    Args:
        rt: 已完成同步装配的 Collector 对象图。

    Notes:
        Runtime 启动失败会向上传播；可选 IEC104 reporting 从站启动失败仅记录
        告警并继续，使采集主链路不因附加上送能力失效而退出。
    """
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

    from wind_hub_core.protocol.ads import router as ads_router

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
    """停止可选 IEC104 从站并优雅停止 Collector Runtime。

    Args:
        rt: 当前 Collector 对象图。
        timeout: Runtime 整体停机硬超时，单位秒。

    Raises:
        TimeoutError: Runtime 未在硬超时内完成优雅停机。

    Notes:
        IEC104 reporting 从站停止失败只记录告警，仍继续释放 Runtime 主资源。
    """
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
        from wind_hub_collector.adapter.outbound.sink.file.csv import FileSink

        return FileSink(cfg)
    if cfg.type == "kafka":
        from wind_hub_collector.adapter.outbound.sink.mq.kafka import KafkaSink

        return KafkaSink(cfg)
    if cfg.type == "db":
        from wind_hub_collector.adapter.outbound.sink.db.postgres import DBSink

        return DBSink(cfg)
    raise ConfigError(
        f"Unknown sink type '{cfg.type}' (available: kafka, file, db)"
    )
