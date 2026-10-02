"""wind-hub-ctl 命令行入口。

本模块只负责参数解析、调用 CollectorClient 和输出 JSON，不读取 Collector
配置，也不直接访问设备。所有业务错误通过 gRPC 状态返回；CLI 将 RPC 错误
写入 stderr，并用非零退出码表示失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import grpc

from wind_hub_ctl.client import CollectorClient


async def _run(args: argparse.Namespace) -> int:
    """执行一次 CLI 子命令。

    Args:
        args: argparse 已解析的命令行参数。

    Returns:
        成功时返回进程退出码 0。

    Raises:
        RuntimeError: 收到解析器未声明的子命令。正常 CLI 路径不会触发。
        grpc.aio.AioRpcError: Collector 不可达、RPC 超时或服务端返回错误状态。
    """
    async with CollectorClient(args.target, timeout=args.rpc_timeout) as client:
        if args.command == "info":
            result = await client.info()
        elif args.command == "status":
            result = await client.status()
        elif args.command == "devices":
            result = await client.devices()
        elif args.command == "tasks":
            result = await client.tasks()
        elif args.command == "task":
            result = await client.task(args.task_id)
        elif args.command == "task-instances":
            result = await client.task_instances()
        elif args.command == "task-instance":
            result = await client.task_instance(args.instance_id)
        else:
            raise RuntimeError(f"unsupported command: {args.command}")
    _print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 wind-hub-ctl 参数解析器。

    Returns:
        包含 Collector endpoint、RPC 超时和只读诊断子命令的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="wind-hub-ctl",
        description="Wind Hub Collector 运行态只读诊断客户端。",
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

    sub.add_parser("tasks", help="列出 Task Definition 与聚合状态")

    task = sub.add_parser("task", help="按 task_id 查询一个 Task")
    task.add_argument("task_id")

    sub.add_parser("task-instances", help="列出全部 Task Instance")
    instance = sub.add_parser("task-instance", help="查询一个 Task Instance")
    instance.add_argument("instance_id")


    return parser


def main() -> int:
    """执行 wind-hub-ctl。

    Returns:
        0 表示命令成功；2 表示 gRPC 调用失败；130 表示用户中断。
    """
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
