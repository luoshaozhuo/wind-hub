"""Collector 组合根。

只装配采集链路：设备会话（Core 协议 Driver）、采集引擎、Sink 注册表与
热重载编排；不包含命令分发、诊断或 Web API。组合根是唯一允许跨层引用
Infrastructure 具体实现的地方：它显式注册 Core 协议 Driver、持有进程级
ADS 本机身份（ADSLocalRouter）、按 enabled 实例化 Sink。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.application import ProtocolRegistry
from core.application.recovery import RecoverySettings
from core.infrastructure import (
    ADSLocalConfig,
    ADSLocalRouter,
)
from core.infrastructure.config import fingerprint_config_set
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver

from .application.config import ADSLocalIdentity, CollectorConfig, DeviceView
from .application.config_service import CollectorConfigService
from .application.identity import CollectorIdentity, build_collector_identity
from .application.metrics_state import CollectorMetricsState
from .application.runtime import CollectorRuntime
from .application.session import CollectorDeviceSession
from .application.sink_port import SinkPort
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
        config_hash: 启动配置集指纹。
        metrics: 进程级采集质量计数与近期事件（gRPC 指标快照的唯一事实源）。
        identity: 本次进程启动身份（collector_id / boot_id / 启动指纹）。
        ads_local_router: 进程级 ADS 本机身份 owner；stop 时关闭。
    """

    boot_config: CollectorConfig
    runtime: CollectorRuntime
    config: CollectorConfigService
    config_dir: Path
    config_hash: str
    metrics: CollectorMetricsState
    identity: CollectorIdentity
    ads_local_router: ADSLocalRouter
    ads_local: ADSLocalConfig | None

    async def start(self) -> None:
        """初始化 ADS 本机身份（配置含 ADS 时）并启动 Runtime。"""
        if self.ads_local is not None:
            await self.ads_local_router.initialize(self.ads_local)
        await self.runtime.start()

    async def stop(self) -> None:
        """停止 Runtime 并释放进程级 ADS 本机身份。"""
        try:
            await self.runtime.stop()
        finally:
            await self.ads_local_router.close()


def _to_ads_local_config(identity: ADSLocalIdentity | None) -> ADSLocalConfig | None:
    """把 Application 层 ADS 本机身份转换为 Core 的 ADSLocalConfig。"""
    if identity is None:
        return None
    return ADSLocalConfig(
        local_ams_net_id=identity.local_ams_net_id,
        local_ip=identity.local_ip,
    )


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
    before_hash = fingerprint_config_set(config_path)
    config = load_collector_config(config_path)
    config_hash = fingerprint_config_set(config_path)
    if before_hash != config_hash:
        raise ValueError(
            "config changed while assembling Collector: "
            f"before={before_hash} after={config_hash}"
        )

    protocol_registry = build_protocol_registry()
    protocol_registry.configure_recovery(
        RecoverySettings(
            reconnect_attempts=config.runtime.reconnect_attempts,
            connect_timeout=config.runtime.connect_timeout,
            read_timeout=config.runtime.read_timeout,
            write_timeout=config.runtime.write_timeout,
        )
    )

    def session_factory(view: DeviceView) -> CollectorDeviceSession:
        protocol = protocol_registry.create(
            view.device.endpoint,
            view.point_table,
            view.options,
        )
        return CollectorDeviceSession(
            view.device,
            view.point_table,
            view.point_meta,
            protocol,
            subscribe_enabled=view.subscribe_enabled,
            supports_scheduled_collection=view.supports_scheduled_collection,
        )

    views = config.device_views()
    devices = {str(device_id): session_factory(view) for device_id, view in views.items()}

    sink_registry = build_sink_registry()
    sinks: dict[str, SinkPort] = {
        name: sink_registry.create(resolved)
        for name, resolved in config.sinks.items()
        if resolved.enabled
    }

    metrics = CollectorMetricsState()
    engine = AcquisitionEngine(
        read_timeout=config.runtime.read_timeout,
        on_points_collected=metrics.observe_collected,
        on_points_bad=metrics.observe_bad,
    )
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
            load_config=lambda: load_collector_config(config_path),
            fingerprint=lambda: fingerprint_config_set(config_path),
            config_hash=config_hash,
        ),
        config_dir=config_path,
        config_hash=config_hash,
        metrics=metrics,
        identity=build_collector_identity(config_hash, collector_id=collector_id),
        ads_local_router=ADSLocalRouter(),
        ads_local=_to_ads_local_config(config.ads_local),
    )


__all__ = [
    "CollectorApp",
    "assemble_collector",
    "build_protocol_registry",
]
