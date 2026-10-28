"""Collector 组合根。

只装配采集链路：设备会话（Core 协议 Driver）、采集引擎、NATS Publisher 与
热重载编排；不包含命令分发、诊断或 Web API。组合根是唯一允许跨层引用
Infrastructure 具体实现的地方：它显式注册 Core 协议 Driver、持有进程级
ADS 本机身份。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.application import ProtocolRegistry
from core.application.settings import ADSLocalConfig, LogSettings
from core.domain import Device, PointTable
from core.infrastructure.config import YamlConfigAdapter, config_dir_digest
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver

from .application.config import CollectorConfig
from .application.config_service import CollectorConfigService
from .application.identity import CollectorIdentity, build_collector_identity
from .application.latest_values import LatestValues
from .application.metrics_state import CollectorMetricsState
from .application.nats_dispatch import SamplePublisher
from .application.runtime import CollectorRuntime
from .application.session import CollectorDeviceSession
from .domain.acquisition import AcquisitionEngine
from .infrastructure.config.loader import collector_config_from_snapshot, load_collector_config
from .infrastructure.nats_publisher import NatsPublisher


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
        runtime: Collector 运行时（Device/Task/Publisher 编排）。
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
    log_settings: LogSettings

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
) -> CollectorApp:
    """从现场配置目录装配 Collector，不执行网络 I/O。

    Args:
        config_dir: 现场配置目录。
        collector_id: 显式 Collector 稳定标识；为空时读取环境变量或主机名。

    Raises:
        ValueError: 装配期间配置目录发生变化（TOCTOU）。
        ConfigError: 配置非法。
    """
    config_path = Path(config_dir)
    config_source = YamlConfigAdapter(config_path)
    before_hash = config_dir_digest(config_path)
    # 启动配置只完整解析一次：装配、日志初始化共用同一份快照。
    snapshot = config_source.load()
    config = collector_config_from_snapshot(snapshot)
    config_hash = config_dir_digest(config_path)
    if before_hash != config_hash:
        raise ValueError(
            "config changed while assembling Collector: "
            f"before={before_hash} after={config_hash}"
        )

    protocol_registry = build_protocol_registry()

    def session_factory(device: Device, point_table: PointTable) -> CollectorDeviceSession:
        protocol = protocol_registry.create(
            device.endpoint,
            point_table,
            device.protocol_options,
        )
        return CollectorDeviceSession(
            device,
            point_table,
            protocol,
            subscribe_enabled=device.subscribe_enabled,
        )

    devices = {
        str(device_id): session_factory(device, config.point_table_for_device(device_id))
        for device_id, device in config.devices.items()
    }

    transport = NatsPublisher(config.messaging.servers, config.messaging.delivery)
    identity = build_collector_identity(config_hash, collector_id=collector_id)
    publisher = SamplePublisher(
        transport,
        wind_farm_id=snapshot.site.site_id,
        identity=identity,
        delivery=config.messaging.delivery,
        max_staleness_ms=config.messaging.max_staleness_ms,
    )
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
        publisher,
        engine,
        config.runtime,
        tasks=dict(config.tasks),
        session_factory=session_factory,
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
        identity=identity,
        ads_local=config.ads_local,
        log_settings=snapshot.system.logs,
    )


__all__ = [
    "CollectorApp",
    "assemble_collector",
    "build_protocol_registry",
]
