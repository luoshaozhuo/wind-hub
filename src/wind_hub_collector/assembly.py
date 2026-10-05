"""Collector 组合根——只装配采集与交付核心。

本模块属于 wind-hub-collector。它只构建 wind-hub-ctl / gRPC 控制面实际
需要的对象图：

Protocol / CollectorDeviceSession / Sink -> AcquisitionEngine -> CollectorRuntime
                           -> CollectorTaskService / CollectorQueryService
                           -> CollectorConfigService / CollectorSinkService

Admin、Overview、Quality、Web API、即时设备通信、现场协议诊断、长期日志与
System Health 不属于 Collector；运行时配置 reload 属于 Collector 核心控制面。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 导入模块以触发内置协议驱动注册；若注册机制改为显式装配，可删除该副作用导入与抑制。
import wind_hub_core.protocol  # noqa: F401
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import CollectorDeviceSession, CollectorRuntime
from wind_hub_collector.application.runtime.metrics_state import CollectorMetricsState
from wind_hub_collector.application.service.config import CollectorConfigService
from wind_hub_collector.application.service.query import CollectorQueryService
from wind_hub_collector.application.service.sink import CollectorSinkService
from wind_hub_collector.application.service.task import CollectorTaskService
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config import Config, DeviceConfig, ResolvedSinkConfig, load_config
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.protocol.port import ProtocolPort
from wind_hub_core.protocol.registry import protocol_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectorApp:
    """Collector 最小运行对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Collector 运行时聚合根。
        engine: 采集执行引擎。
        sinks: 已装配的 Sink 实例注册表。
        tasks: Task / Task Instance 控制服务。
        query: 只读查询服务。
        config: 本地 YAML 增量热重载服务。
        sink_service: 运行 Sink 检查与诊断写服务。
    """

    boot_config: Config
    runtime: CollectorRuntime
    engine: AcquisitionEngine
    sinks: dict[str, SinkPort]
    tasks: CollectorTaskService
    query: CollectorQueryService
    config: CollectorConfigService
    sink_service: CollectorSinkService
    metrics_state: CollectorMetricsState


def assemble(
    config_dir: str | Path,
    sink_factory: Callable[[ResolvedSinkConfig], SinkPort] | None = None,
) -> CollectorApp:
    """从 YAML 配置同步装配 Collector，不执行网络 I/O。

    Args:
        config_dir: 现场配置目录。
        sink_factory: 可选 Sink 工厂；测试或定制部署可注入替代实现。

    Returns:
        完整但尚未启动的 CollectorApp。

    Raises:
        ConfigError: 配置缺失、跨文件引用非法或 Sink 类型未知。
        Exception: 协议/Sink 构造阶段的其他配置型异常原样传播。

    Notes:
        本函数只构造对象图和内存映射；设备连接与 Sink open 均在
        start_runtime 阶段发生。
    """
    cfg = load_config(config_dir)
    make_sink = sink_factory or _create_sink

    devices: dict[str, CollectorDeviceSession] = {}
    for device_cfg in cfg.devices.devices:
        protocol = _create_protocol(device_cfg)
        points = cfg.points_for_device(device_cfg.device_id)
        devices[device_cfg.device_id] = CollectorDeviceSession(
            config=device_cfg,
            points=points,
            protocol=protocol,
        )

    sinks = {sink.name: make_sink(sink) for sink in cfg.sinks.sinks if sink.enabled}

    metrics_state = CollectorMetricsState()
    engine = AcquisitionEngine(
        read_timeout=cfg.system.runtime.read_timeout,
    )
    engine.add_observer(metrics_state.observe_points)

    runtime = CollectorRuntime(
        devices=devices,
        sinks=sinks,
        engine=engine,
        config=cfg.system.runtime,
        tasks={task.task_id: task for task in cfg.tasks.tasks},
        protocol_factory=_create_protocol,
        sink_factory=make_sink,
        metrics_hook=metrics_state,
    )

    tasks = CollectorTaskService(runtime)
    query = CollectorQueryService(runtime)
    config = CollectorConfigService(config_dir, runtime, cfg)
    sink_service = CollectorSinkService(runtime)

    return CollectorApp(
        boot_config=cfg,
        runtime=runtime,
        engine=engine,
        sinks=sinks,
        tasks=tasks,
        query=query,
        config=config,
        sink_service=sink_service,
        metrics_state=metrics_state,
    )


async def start_runtime(rt: CollectorApp) -> None:
    """启动 CollectorRuntime。"""
    await _maybe_init_ads_local(rt)
    await rt.runtime.start()


async def _maybe_init_ads_local(rt: CollectorApp) -> None:
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
    rt: CollectorApp,
    timeout: float = 30.0,
) -> None:
    """在硬超时内优雅停止 CollectorRuntime。"""
    await asyncio.wait_for(rt.runtime.stop(), timeout=timeout)


def _create_protocol(cfg: DeviceConfig) -> ProtocolPort:
    """按协议注册表创建设备驱动。"""
    return protocol_registry.create(cfg.protocol, cfg)


def _create_sink(cfg: ResolvedSinkConfig) -> SinkPort:
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
    if cfg.type == "iec104":
        from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink

        return IEC104Sink(cfg)
    if cfg.type == "modbus":
        from wind_hub_collector.adapter.outbound.sink.modbus import ModbusSink

        return ModbusSink(cfg)
    raise ConfigError(
        f"Unknown sink type '{cfg.type}' "
        "(available: kafka, file, db, iec104, modbus)"
    )
