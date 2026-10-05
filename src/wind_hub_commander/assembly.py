"""Commander 组合根。

只装配即时设备通信、命令分发、读取和诊断服务，不包含采集 Task、Sink、
Web API 或 Collector Runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import wind_hub_core.protocol  # noqa: F401 — 触发内置 Driver 注册
from wind_hub_commander.application import (
    CommanderCommandService,
    CommanderConfigService,
    CommanderDiagnosticService,
    CommanderReadService,
)
from wind_hub_commander.config import CommanderConfig, load_commander_config
from wind_hub_commander.dispatcher import CommandDispatcher
from wind_hub_commander.runtime import CommanderRuntime
from wind_hub_core.config import fingerprint_config_set


@dataclass(slots=True)
class CommanderApp:
    """Commander 进程对象图。

    Attributes:
        boot_config: 进程启动时加载的配置快照；仅表示启动基线。
        runtime: Commander 运行时。
        dispatcher: 命令分发器。
        command: 即时写入服务。
        read: 即时读取服务。
        diagnostic: 诊断服务。
        config: 配置事务服务（prepare / activate / abort）。
        config_dir: 现场配置目录。
    """

    boot_config: CommanderConfig
    runtime: CommanderRuntime
    dispatcher: CommandDispatcher
    command: CommanderCommandService
    read: CommanderReadService
    diagnostic: CommanderDiagnosticService
    config: CommanderConfigService
    config_dir: Path


def assemble_commander(config_dir: str | Path) -> CommanderApp:
    """从现场配置目录装配 Commander，不执行网络 I/O。"""
    config_path = Path(config_dir)
    before_hash = fingerprint_config_set(config_path)
    config = load_commander_config(config_path)
    config_hash = fingerprint_config_set(config_path)
    if before_hash != config_hash:
        raise ValueError(
            "config changed while assembling Commander: "
            f"before={before_hash} after={config_hash}"
        )
    runtime = CommanderRuntime(config, config_hash=config_hash)
    dispatcher = CommandDispatcher(
        runtime,
        default_timeout=config.write_timeout,
    )
    return CommanderApp(
        boot_config=config,
        runtime=runtime,
        dispatcher=dispatcher,
        command=CommanderCommandService(dispatcher),
        read=CommanderReadService(runtime),
        diagnostic=CommanderDiagnosticService(runtime),
        config=CommanderConfigService(config_path, runtime),
        config_dir=config_path,
    )
