"""Seahorse 统一 gRPC 遥测发布和虚拟设备命令入口。"""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress
from pathlib import Path

from industrial_gateway.infrastructure.config.loader import load_config as load_gateway
from seahorse.application.command_queue import CommandQueue
from seahorse.infrastructure.config import SeahorseConfig, load_config
from seahorse.infrastructure.factory import create_runtime
from seahorse.infrastructure.grpc_command_server import start_command_server
from seahorse.infrastructure.grpc_publisher import GrpcSnapshotPublisher


async def _run(
    config: SeahorseConfig,
    publisher: GrpcSnapshotPublisher | None,
    command_bind: str | None,
    steps: int | None,
) -> None:
    commands = CommandQueue() if command_bind else None
    server = None
    if commands is not None and command_bind is not None:
        server = await start_command_server(
            commands, frozenset(item.device_id for item in config.turbines), command_bind
        )
    runtime = create_runtime(config, publisher=publisher, commands=commands)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, shutdown.set)
    try:
        worker = asyncio.create_task(asyncio.to_thread(runtime.run, steps))
        stopper = asyncio.create_task(shutdown.wait())
        try:
            done, _ = await asyncio.wait((worker, stopper), return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                runtime.stop()
            await worker
        except asyncio.CancelledError:
            runtime.stop()
            await worker
            raise
        finally:
            stopper.cancel()
    finally:
        runtime.stop()
        if commands is not None:
            commands.close()
        if server is not None:
            await server.stop(grace=0)
        if publisher is not None:
            publisher.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="seahorse")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--gateway-grpc-endpoint", type=str)
    parser.add_argument("--gateway-config", type=Path)
    parser.add_argument("--command-bind", type=str)
    args = parser.parse_args()
    config = load_config(args.config)
    if bool(args.gateway_grpc_endpoint) != bool(args.gateway_config):
        parser.error("--gateway-grpc-endpoint and --gateway-config must be specified together")
    publisher = None
    if args.gateway_config is not None:
        gateway = load_gateway(args.gateway_config)
        if gateway.data_grpc_bind != args.gateway_grpc_endpoint:
            parser.error("Gateway data gRPC bind and Seahorse endpoint must match")
        feedback = {
            device_id: frozenset(
                name
                for name, point in gateway.point_tables[device.point_table_id].points.items()
                if point.role == "feedback"
            )
            for device_id, device in gateway.devices.items()
        }
        # 使用独立模型实例预演首步，在开放控制入口之前验证所有反馈点。
        snapshot = create_runtime(config).step()
        by_device = {device.device_id: device.points for device in snapshot.devices}
        for device_id, names in feedback.items():
            actual = by_device.get(device_id)
            if actual is None:
                parser.error(f"Gateway device is missing from Seahorse: {device_id}")
            missing = names - actual.keys()
            if missing:
                parser.error(f"Gateway feedback is missing: {device_id}.{sorted(missing)}")
        publisher = GrpcSnapshotPublisher(args.gateway_grpc_endpoint, feedback)
    if not args.check:
        asyncio.run(_run(config, publisher, args.command_bind, args.steps))
    elif publisher is not None:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
