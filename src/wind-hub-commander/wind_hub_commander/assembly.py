"""Commander 组合根。

只装配即时设备通信、命令分发、读取和诊断用例，不包含采集 Task、Sink、
Web API 或 Collector Runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import wind_hub_core.protocol  # noqa: F401 — 触发内置 Driver 注册

from wind_hub_commander.application import CommandUseCase, DiagnosticUseCase, ReadUseCase
from wind_hub_commander.config import CommanderConfig, load_commander_config
from wind_hub_commander.dispatcher import CommandDispatcher
from wind_hub_commander.runtime import CommanderRuntime


@dataclass(slots=True)
class CommanderApp:
    """Commander 进程对象图。"""

    config: CommanderConfig
    runtime: CommanderRuntime
    dispatcher: CommandDispatcher
    command: CommandUseCase
    read: ReadUseCase
    diagnostic: DiagnosticUseCase
    config_dir: Path


def assemble_commander(config_dir: str | Path) -> CommanderApp:
    """从现场配置目录装配 Commander，不执行网络 I/O。"""
    config = load_commander_config(config_dir)
    runtime = CommanderRuntime(config)
    dispatcher = CommandDispatcher(
        runtime,
        default_timeout=config.write_timeout,
    )
    return CommanderApp(
        config=config,
        runtime=runtime,
        dispatcher=dispatcher,
        command=CommandUseCase(runtime, dispatcher),
        read=ReadUseCase(runtime),
        diagnostic=DiagnosticUseCase(runtime),
        config_dir=Path(config_dir),
    )
