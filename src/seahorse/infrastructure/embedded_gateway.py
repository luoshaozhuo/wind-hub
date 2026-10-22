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
    # 仅虚拟场站内存镜像，不构成现场控制命令。
    turbine_ratings = {
        turbine.device_id: turbine.rated_power_kw for turbine in config.turbines
    }
    initial: dict[tuple[str, str], bool | int | float] = {}
    for device_id, device in gateway_config.devices.items():
        table = gateway_config.point_tables[device.point_table_id]
        for point_id, point in table.points.items():
            if point.role != "command":
                continue
            value: bool | int | float
            if point.data_type == "bool":
                value = False
            elif point_id == "ctrlActPow":
                value = turbine_ratings[device_id]
            elif point_id == "emsActPowerChangeRate":
                value = 200.0
            elif point.data_type.startswith(("int", "uint")):
                value = 0
            else:
                value = 0.0
            initial[(device_id, point_id)] = value
    gateway.store.seed_virtual_command_mirrors(initial)
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
        worker = asyncio.create_task(asyncio.to_thread(runtime.run, steps))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            runtime.stop()
            await worker
            raise
    finally:
        if commands is not None:
            commands.close()
        await gateway.stop()
        if rpc is not None:
            await rpc.stop(grace=0)
