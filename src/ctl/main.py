"""wind-hub-ctl 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from uuid import uuid4

import grpc

from .client import CollectorClient, CommanderClient


def _print(payload: dict[str, Any]) -> None:
    """以 JSON 输出一次操作结果。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_scalar(text: str) -> object:
    """把命令行文本解析为 JSON 标量；无法解析时按字符串处理。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(value, (dict, list)) or value is None:
        raise ValueError("value must be bool/int/float/string")
    return value


def _add_config_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """加入 prepare / activate / abort 配置事务命令。"""
    prepare = sub.add_parser("prepare-config")
    prepare.add_argument("revision_id")
    prepare.add_argument("config_hash")

    activate = sub.add_parser("activate-config")
    activate.add_argument("revision_id")

    abort = sub.add_parser("abort-config")
    abort.add_argument("revision_id")


def _build_collector_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """构建 Collector 子命令。"""
    parser = sub.add_parser("collector", help="直接操控 Collector")
    parser.add_argument("--target", default="127.0.0.1:50051")
    commands = parser.add_subparsers(dest="operation", required=True)

    for name in ("info", "status", "metrics", "devices", "sinks", "tasks", "instances"):
        commands.add_parser(name)

    sink = commands.add_parser("verify-sink")
    sink.add_argument("name")
    sink = commands.add_parser("test-sink")
    sink.add_argument("name")

    task = commands.add_parser("task")
    task.add_argument("task_id")
    instance = commands.add_parser("instance")
    instance.add_argument("instance_id")

    placement = commands.add_parser("placement")
    placement.add_argument("worker_id")
    placement.add_argument("generation", type=int)
    placement.add_argument("task_ids", nargs="+")

    start_task = commands.add_parser("start-task")
    start_task.add_argument("task_id")
    start_task.add_argument("--generation", type=int, required=True)
    stop_task = commands.add_parser("stop-task")
    stop_task.add_argument("task_id")

    start_instance = commands.add_parser("start-instance")
    start_instance.add_argument("instance_id")
    start_instance.add_argument("--generation", type=int, required=True)
    stop_instance = commands.add_parser("stop-instance")
    stop_instance.add_argument("instance_id")

    _add_config_commands(commands)
    prepare = commands.choices["prepare-config"]
    prepare.add_argument("--force", action="store_true")


def _build_commander_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """构建 Commander 子命令。"""
    parser = sub.add_parser("commander", help="直接操控 Commander")
    parser.add_argument("--target", default="127.0.0.1:50052")
    commands = parser.add_subparsers(dest="operation", required=True)

    commands.add_parser("status")
    commands.add_parser("devices")

    read = commands.add_parser("read")
    read.add_argument("device_id")
    read.add_argument("point_id")

    read_many = commands.add_parser("read-many")
    read_many.add_argument("device_id")
    read_many.add_argument("point_ids", nargs="+")

    write = commands.add_parser("write")
    write.add_argument("device_id")
    write.add_argument("point_id")
    write.add_argument("value", type=_parse_scalar)
    write.add_argument("--command-id", default=None)
    write.add_argument("--io-timeout", type=float, default=1.0)

    write_many = commands.add_parser("write-many")
    write_many.add_argument(
        "--item",
        action="append",
        nargs=3,
        metavar=("DEVICE", "POINT", "VALUE"),
        required=True,
    )
    write_many.add_argument("--io-timeout", type=float, default=1.0)

    verify_device = commands.add_parser("verify-device")
    verify_device.add_argument("device_id")
    verify_device.add_argument("--io-timeout", type=float, default=1.0)

    for name in ("resolve-point", "verify-point"):
        point = commands.add_parser(name)
        point.add_argument("device_id")
        point.add_argument("point_id")

    verify_points = commands.add_parser("verify-points")
    verify_points.add_argument("device_id")
    verify_points.add_argument("--point-group", default="")

    _add_config_commands(commands)


def build_parser() -> argparse.ArgumentParser:
    """构建完整 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub-ctl",
        description="直接连接 Collector / Commander 的本地运维 CLI。",
    )
    parser.add_argument("--rpc-timeout", type=float, default=5.0)
    services = parser.add_subparsers(dest="service", required=True)
    _build_collector_parser(services)
    _build_commander_parser(services)
    return parser


def _collector_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """提取 Collector 操作参数。"""
    keys = (
        "name",
        "task_id",
        "instance_id",
        "worker_id",
        "generation",
        "task_ids",
        "revision_id",
        "config_hash",
        "force",
    )
    return {key: getattr(args, key) for key in keys if hasattr(args, key)}


def _commander_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """提取 Commander 操作参数并规范化批量写命令。"""
    if args.operation == "write":
        return {
            "device_id": args.device_id,
            "point_id": args.point_id,
            "value": args.value,
            "command_id": args.command_id or uuid4().hex,
            "io_timeout": args.io_timeout,
        }
    if args.operation == "write-many":
        items = [
            {
                "device_id": device_id,
                "point_id": point_id,
                "value": _parse_scalar(value),
                "command_id": uuid4().hex,
                "timeout": args.io_timeout,
            }
            for device_id, point_id, value in args.item
        ]
        return {"items": items}

    keys = (
        "device_id",
        "point_id",
        "point_ids",
        "point_group",
        "io_timeout",
        "revision_id",
        "config_hash",
    )
    return {key: getattr(args, key) for key in keys if hasattr(args, key)}


async def _run(args: argparse.Namespace) -> int:
    """执行一次 CLI 命令。"""
    if args.service == "collector":
        async with CollectorClient(args.target, timeout=args.rpc_timeout) as client:
            result = await client.call(args.operation, **_collector_kwargs(args))
    else:
        async with CommanderClient(args.target, timeout=args.rpc_timeout) as client:
            result = await client.call(args.operation, **_commander_kwargs(args))
    _print(result)
    return 0


def main() -> int:
    """执行 wind-hub-ctl。"""
    args = build_parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except (grpc.aio.AioRpcError, ValueError) as exc:
        if isinstance(exc, grpc.aio.AioRpcError):
            detail = f"{exc.code().name}: {exc.details()}"
        else:
            detail = str(exc)
        print(f"ctl failed: {detail}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
