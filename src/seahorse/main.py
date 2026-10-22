"""Seahorse 独立命令行入口。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from seahorse.application.command_queue import CommandQueue
from seahorse.infrastructure.grpc_command_server import start_command_server
from seahorse.infrastructure.config import load_config
from seahorse.infrastructure.factory import create_runtime
from seahorse.infrastructure.gateway_mapping import validate_gateway_mapping
from seahorse.infrastructure.modbus_publisher import GatewayModbusPublisher, load_publish_config


async def _run_command_service(config: object, args: argparse.Namespace) -> None:
    from seahorse.infrastructure.config import SeahorseConfig

    if not isinstance(config, SeahorseConfig):
        raise TypeError("invalid configuration")
    commands = CommandQueue()
    server = await start_command_server(
        commands, frozenset(item.device_id for item in config.turbines), args.command_bind
    )
    try:
        runtime = create_runtime(config, publisher=args.publisher, commands=commands)
        await asyncio.to_thread(runtime.run, args.steps)
    finally:
        await server.stop(grace=0)


def main() -> int:
    parser = argparse.ArgumentParser(prog="seahorse")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--gateway-map", type=Path)
    parser.add_argument("--gateway-config", type=Path)
    parser.add_argument("--command-bind", type=str)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.gateway_config is not None and args.gateway_map is None:
        parser.error("--gateway-config requires --gateway-map")
    publisher = None
    if args.gateway_map is not None:
        mapping = load_publish_config(args.gateway_map)
        if args.gateway_config is not None:
            validate_gateway_mapping(mapping, args.gateway_config)
        publisher = GatewayModbusPublisher(mapping)
    if not args.check:
        if args.command_bind:
            args.publisher = publisher
            asyncio.run(_run_command_service(config, args))
        else:
            create_runtime(config, publisher=publisher).run(steps=args.steps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
