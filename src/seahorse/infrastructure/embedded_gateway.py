"""Seahorse 与 Industrial Gateway 共享进程的集成部署入口。

虚拟机组运行在工作线程；Modbus 服务、命令 RPC 与 PointStore 在主事件循环。
仅供虚拟场站；真实生产 Gateway 不应接入此进程。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.config.loader import load_config as load_gateway_config
from seahorse.application.command_queue import CommandQueue
from seahorse.infrastructure.config import SeahorseConfig
from seahorse.infrastructure.factory import create_runtime
from seahorse.infrastructure.gateway_store_publisher import GatewayStorePublisher
from seahorse.infrastructure.grpc_command_server import start_command_server


async def run_embedded(
    config: SeahorseConfig,
    gateway_path: Path,
    *,
    command_bind: str | None,
    steps: int | None,
) -> None:
    gateway_config = load_gateway_config(gateway_path)
    if gateway_config.control.enabled:
        if command_bind is None:
            raise ValueError("control-enabled gateway requires --command-bind")
        if gateway_config.control.commander.endpoint != command_bind:
            raise ValueError("Gateway Commander endpoint must equal Seahorse command bind")
    gateway = assemble_gateway(gateway_config)
    loop = asyncio.get_running_loop()
    publisher = GatewayStorePublisher(gateway.store, gateway_config, loop)
    commands = CommandQueue() if command_bind is not None else None
    rpc = None
    try:
        if commands is not None and command_bind is not None:
            rpc = await start_command_server(
                commands,
                frozenset(turbine.device_id for turbine in config.turbines),
                command_bind,
            )
        await gateway.start()
        runtime = create_runtime(config, publisher=publisher, commands=commands)
        await asyncio.to_thread(runtime.run, steps)
    finally:
        if commands is not None:
            commands.close()
        await gateway.stop()
        if rpc is not None:
            await rpc.stop(grace=0)
