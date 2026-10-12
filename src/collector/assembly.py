"""Collector 组合根。

只装配采集链路：设备会话（Core 协议 Driver）、采集引擎、Sink 注册表与
热重载编排；不包含命令分发、诊断或 Web API。组合根是唯一允许跨层引用
Infrastructure 具体实现的地方：它显式注册 Core 协议 Driver、持有进程级
ADS 本机身份、按 enabled 实例化 Sink。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.application import ProtocolRegistry
from core.application.port import SinkPort
from core.application.settings import ADSLocalConfig
from core.infrastructure.config import YamlConfigAdapter, config_dir_digest
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver

from .application.config import CollectorConfig, DeviceView
from .application.config_service import CollectorConfigService
from .application.identity import CollectorIdentity, build_collector_identity
from .application.latest_values import LatestValues
from .application.metrics_state import CollectorMetricsState
from .application.runtime import CollectorRuntime
from .application.session import CollectorDeviceSession
from .application.sink_registry import SinkFactory
from .domain.acquisition import AcquisitionEngine
from .infrastructure.config.loader import load_collector_config
from .infrastructure.sink import build_sink_registry


def build_protocol_registry() -> ProtocolRegistry:
    """显式注册 Collector 使用的 Core 协议 Driver。"""
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)
    return registry


@dataclass(slots=True)
class CollectorApp:
    """Collector 进程对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Collector 运行时（Device/Task/Sink 编排）。
        config: 配置热重载编排服务。
        config_dir: 现场配置目录。
        config_hash: 启动配置集摘要。
        metrics: 进程级采集质量计数与近期事件（gRPC 指标快照的唯一事实源）。
        identity: 本次进程启动身份（collector_id / boot_id / 启动摘要）。
    """

    boot_config: CollectorConfig
    runtime: CollectorRuntime
    config: CollectorConfigService
    config_dir: Path
    config_hash: str
    metrics: CollectorMetricsState
    latest_values: LatestValues
    identity: CollectorIdentity
    ads_local: ADSLocalConfig | None

    async def start(self) -> None:
        """初始化 ADS 本机身份（配置含 ADS 时）并启动 Runtime。"""
        if self.ads_local is not None:
            await ADSDriver.initialize_local(self.ads_local)
        await self.runtime.start()

    async def stop(self) -> None:
        """停止 Runtime 并释放进程级 ADS 本机身份。"""
        try:
            await self.runtime.stop()
        finally:
            await ADSDriver.close_local()


def assemble_collector(
    config_dir: str | Path,
    *,
    collector_id: str | None = None,
    sink_factories: Mapping[str, SinkFactory] | None = None,
) -> CollectorApp:
    """从现场配置目录装配 Collector，不执行网络 I/O。

    Args:
        config_dir: 现场配置目录。
        collector_id: 显式 Collector 稳定标识；为空时读取环境变量或主机名。
        sink_factories: 按类型覆盖内置 Sink factory（性能/soak 等资格工具
            注入 NullSink 或计量包装的 seam）；键必须是已注册的 Sink 类型。

    Raises:
        ValueError: 装配期间配置目录发生变化（TOCTOU）。
        ConfigError: 配置非法。
    """
    config_path = Path(config_dir)
    config_source = YamlConfigAdapter(config_path)
    before_hash = config_dir_digest(config_path)
    config = load_collector_config(config_source)
    config_hash = config_dir_digest(config_path)
    if before_hash != config_hash:
        raise ValueError(
            "config changed while assembling Collector: "
            f"before={before_hash} after={config_hash}"
        )

    protocol_registry = build_protocol_registry()

    def session_factory(view: DeviceView) -> CollectorDeviceSession:
        protocol = protocol_registry.create(
            view.device.endpoint,
            view.point_table,
            view.options,
        )
        return CollectorDeviceSession(
            view.device,
            view.point_table,
            protocol,
            subscribe_enabled=view.subscribe_enabled,
        )

    views = config.device_views()
    devices = {str(device_id): session_factory(view) for device_id, view in views.items()}

    sink_registry = build_sink_registry()
    for type_name, factory in (sink_factories or {}).items():
        sink_registry.override(type_name, factory)
    sinks: dict[str, SinkPort] = {
        name: sink_registry.create(resolved)
        for name, resolved in config.sinks.items()
        if resolved.enabled
    }

    metrics = CollectorMetricsState()
    latest_values = LatestValues()
    engine = AcquisitionEngine(
        read_timeout=config.runtime.read_timeout,
        on_points_collected=metrics.observe_collected,
        on_points_bad=metrics.observe_bad,
    )
    engine.add_observer(latest_values.update)
    runtime = CollectorRuntime(
        devices,
        {str(device_id): view for device_id, view in views.items()},
        sinks,
        engine,
        config.runtime,
        tasks=dict(config.tasks),
        session_factory=session_factory,
        sink_factory=sink_registry.create,
        metrics_hook=metrics,
    )

    return CollectorApp(
        boot_config=config,
        runtime=runtime,
        config=CollectorConfigService(
            runtime,
            config,
            load_config=lambda: load_collector_config(config_source),
            config_digest=lambda: config_dir_digest(config_path),
            config_hash=config_hash,
            latest_values=latest_values,
        ),
        config_dir=config_path,
        config_hash=config_hash,
        metrics=metrics,
        latest_values=latest_values,
        identity=build_collector_identity(config_hash, collector_id=collector_id),
        ads_local=config.ads_local,
    )


__all__ = [
    "CollectorApp",
    "assemble_collector",
    "build_protocol_registry",
]
