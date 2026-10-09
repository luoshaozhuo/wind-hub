"""Commander 组合根。

只装配即时设备通信、命令分发、读取、诊断和配置事务；不包含采集 Task、
Sink、Web API 或 Collector Runtime。组合根是唯一允许跨层引用
Infrastructure 具体实现的地方：它显式注册 Core 协议 Driver、持有进程级
ADS 本机身份（ADSLocalRouter）、装配诊断 probe。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.domain import ConnectionEndpoint, Device, ProtocolOptions
from core.infrastructure import (
    ADSLocalConfig,
    ADSLocalRouter,
    ProtocolRegistry,
)
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver

from .application.config import CommanderConfig
from .application.diagnostic import (
    CommanderDiagnosticService,
    SymbolProbe,
)
from .application.dispatcher import CommandDispatcher
from .application.runtime import CommanderRuntime
from .application.services import (
    CommanderConfigService,
    CommanderReadService,
)
from .infrastructure.config import (
    fingerprint_config_set,
    load_commander_config,
)
from .infrastructure.probe import (
    AdsDiagnosticProbe,
    ping_host,
    tcp_port_open,
)


@dataclass(slots=True)
class CommanderApp:
    """Commander 进程对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Commander 运行时（generation 生命周期）。
        dispatcher: 命令分发器（即时写入入口，含幂等与批量并发）。
        read: 即时读取服务。
        diagnostic: 诊断服务。
        config: 配置事务服务（prepare / activate / abort）。
        config_dir: 现场配置目录。
        config_hash: 启动配置集指纹。
        ads_local_router: 进程级 ADS 本机身份 owner；stop 时关闭。
    """

    boot_config: CommanderConfig
    runtime: CommanderRuntime
    dispatcher: CommandDispatcher
    read: CommanderReadService
    diagnostic: CommanderDiagnosticService
    config: CommanderConfigService
    config_dir: Path
    config_hash: str
    ads_local_router: ADSLocalRouter

    async def start(self) -> None:
        """启动 Runtime（协议环境准备）。"""
        await self.runtime.start()

    async def stop(self) -> None:
        """停止 Runtime 并释放进程级 ADS 本机身份。"""
        try:
            await self.runtime.stop()
        finally:
            await self.ads_local_router.close()


def build_protocol_registry() -> ProtocolRegistry:
    """显式注册 Commander 使用的 Core 协议 Driver。"""
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)
    return registry


def assemble_commander(config_dir: str | Path) -> CommanderApp:
    """从现场配置目录装配 Commander，不执行网络 I/O。

    Raises:
        ValueError: 装配期间配置目录发生变化（TOCTOU）。
        ConfigError: 配置非法。
    """
    config_path = Path(config_dir)
    before_hash = fingerprint_config_set(config_path)
    config = load_commander_config(config_path)
    config_hash = fingerprint_config_set(config_path)
    if before_hash != config_hash:
        raise ValueError(
            "config changed while assembling Commander: "
            f"before={before_hash} after={config_hash}"
        )

    ads_local_router = ADSLocalRouter()

    async def prepare_protocols(current: CommanderConfig) -> None:
        """协议环境准备：初始化进程级 ADS 本机身份（配置含 ADS 时）。"""
        if current.ads_local is None:
            return
        await ads_local_router.initialize(
            ADSLocalConfig(
                local_ams_net_id=current.ads_local.local_ams_net_id,
                local_ip=current.ads_local.local_ip,
            )
        )

    def probe_factory(
        device: Device,
        endpoint: ConnectionEndpoint,
        options: ProtocolOptions,
    ) -> SymbolProbe | None:
        """诊断 probe 工厂——只创建 ADS 探测会话，不读取启动配置。

        协议判定由 DiagnosticService 按 DeviceSession.protocol_name 完成；
        endpoint/options 均来自调用方固定 generation 的事实。
        """
        del device
        return AdsDiagnosticProbe(endpoint, options)

    runtime = CommanderRuntime(
        config,
        config_hash=config_hash,
        protocol_registry=build_protocol_registry(),
        prepare_protocols=prepare_protocols,
    )
    dispatcher = CommandDispatcher(runtime)
    return CommanderApp(
        boot_config=config,
        runtime=runtime,
        dispatcher=dispatcher,
        read=CommanderReadService(runtime),
        diagnostic=CommanderDiagnosticService(
            runtime,
            ping=lambda host, timeout: ping_host(host, timeout=timeout),
            tcp_connect=lambda host, port, timeout: tcp_port_open(host, port, timeout=timeout),
            probe_factory=probe_factory,
        ),
        config=CommanderConfigService(
            config_path,
            runtime,
            load_config=load_commander_config,
            fingerprint=fingerprint_config_set,
        ),
        config_dir=config_path,
        config_hash=config_hash,
        ads_local_router=ads_local_router,
    )


__all__ = [
    "CommanderApp",
    "assemble_commander",
    "build_protocol_registry",
]
