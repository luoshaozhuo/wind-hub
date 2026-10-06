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

from wind_hub_collector.adapter.outbound.sink import build_sink_registry
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import CollectorDeviceSession, CollectorRuntime
from wind_hub_collector.application.runtime.metrics_state import CollectorMetricsState
from wind_hub_collector.application.service.config import CollectorConfigService
from wind_hub_collector.application.service.query import CollectorQueryService
from wind_hub_collector.application.service.sink import CollectorSinkService
from wind_hub_collector.application.service.task import CollectorTaskService
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config import Config, ResolvedSinkConfig, load_config
from wind_hub_core.device import create_device_session
from wind_hub_core.protocol import build_protocol_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectorApp:
    """Collector 最小运行对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Collector 运行时聚合根——设备/Sink 注册表与采集引擎经其
            只读视图观察，不在本对象重复暴露第二份引用。
        tasks: Task / Task Instance 控制服务。
        query: 只读查询服务。
        config: 本地 YAML 增量热重载服务。
        sink_service: 运行 Sink 检查与诊断写服务。
        metrics_state: 采集指标聚合状态（gRPC 控制面快照数据源）。
    """

    boot_config: Config
    runtime: CollectorRuntime
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
    protocol_registry = build_protocol_registry()
    sinks_registry = build_sink_registry()
    make_sink = sink_factory or sinks_registry.create

    devices: dict[str, CollectorDeviceSession] = {}
    for device_cfg in cfg.devices.values():
        devices[device_cfg.device_id] = create_device_session(
            device_cfg,
            cfg.points_for_device(device_cfg.device_id),
            protocol_registry,
            session_type=CollectorDeviceSession,
        )

    sinks = {name: make_sink(sink) for name, sink in cfg.sinks.items() if sink.enabled}

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
        tasks=dict(cfg.tasks),
        protocol_factory=protocol_registry.create_for,
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
        tasks=tasks,
        query=query,
        config=config,
        sink_service=sink_service,
        metrics_state=metrics_state,
    )


async def start_runtime(app: CollectorApp) -> None:
    """启动 CollectorRuntime。"""
    await _maybe_init_ads_local(app)
    await app.runtime.start()


async def _maybe_init_ads_local(app: CollectorApp) -> None:
    """存在 ADS 设备时执行一次进程级本机 AMS 初始化。"""
    ads_cfg = app.boot_config.system.ads
    if ads_cfg is None:
        return
    if not any(
        device.protocol == "ads"
        for device in app.boot_config.devices.values()
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
    app: CollectorApp,
    timeout: float = 30.0,
) -> None:
    """在硬超时内优雅停止 CollectorRuntime。"""
    await asyncio.wait_for(app.runtime.stop(), timeout=timeout)
