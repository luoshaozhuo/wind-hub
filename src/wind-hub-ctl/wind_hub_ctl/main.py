"""wind-hub-ctl 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import grpc

from wind_hub_ctl.client import CollectorClient


def _json_value(raw: str) -> Any:
    """把 CLI 值按 JSON 标量解析；解析失败时保留字符串。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _print(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


async def _run(args: argparse.Namespace) -> int:
    async with CollectorClient(args.target, timeout=args.rpc_timeout) as client:
        if args.command == "info":
            result = await client.info()
        elif args.command == "status":
            result = await client.status()
        elif args.command == "devices":
            result = await client.devices()
        elif args.command == "read":
            result = await client.read(args.device_id, args.point_id)
        elif args.command == "tasks":
            result = await client.tasks()
        elif args.command == "task":
            result = await client.task(args.instance_id)
        elif args.command == "start":
            result = await client.start_task(args.instance_id)
        elif args.command == "stop":
            result = await client.stop_task(args.instance_id)
        elif args.command == "start-all":
            result = await client.start_all()
        elif args.command == "stop-all":
            result = await client.stop_all()
        elif args.command == "write":
            result = await client.write(
                args.device_id,
                args.point_id,
                _json_value(args.value),
                timeout=args.write_timeout,
            )
        else:
            raise RuntimeError(f"unsupported command: {args.command}")
    _print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 wind-hub-ctl 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub-ctl",
        description="Wind Hub Collector gRPC 控制客户端。",
    )
    parser.add_argument(
        "--target",
        default="127.0.0.1:50051",
        help="Collector gRPC endpoint，默认 127.0.0.1:50051",
    )
    parser.add_argument(
        "--rpc-timeout",
        type=float,
        default=5.0,
        help="单次 RPC 超时（秒）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="查询 Collector 基本信息")
    sub.add_parser("status", help="查询 Runtime 状态")
    sub.add_parser("devices", help="列出设备运行状态")

    read = sub.add_parser("read", help="即时读取单个设备点位")
    read.add_argument("device_id")
    read.add_argument("point_id")

    sub.add_parser("tasks", help="列出 Task Instance")

    task = sub.add_parser("task", help="查询一个 Task Instance")
    task.add_argument("instance_id")

    start = sub.add_parser("start", help="启动一个 Task Instance")
    start.add_argument("instance_id")

    stop = sub.add_parser("stop", help="停止一个 Task Instance")
    stop.add_argument("instance_id")

    sub.add_parser("start-all", help="启动全部已分配 Task Instance")
    sub.add_parser("stop-all", help="停止全部已分配 Task Instance")

    write = sub.add_parser("write", help="向设备点位写值")
    write.add_argument("device_id")
    write.add_argument("point_id")
    write.add_argument(
        "value",
        help='按 JSON 标量解析，例如 12.5、true、"text"；普通文本保持字符串',
    )
    write.add_argument(
        "--write-timeout",
        type=float,
        default=5.0,
        help="设备写操作超时（秒）",
    )
    return parser


def main() -> int:
    """同步 CLI 入口。"""
    args = build_parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except grpc.aio.AioRpcError as exc:
        print(
            f"RPC failed: {exc.code().name}: {exc.details()}",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
