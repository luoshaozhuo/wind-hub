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


def _json_value(raw: str) -> Any:
    """把 CLI 文本解析为 JSON 标量；非 JSON 文本保持字符串。

    Args:
        raw: 命令行传入的原始字符串。

    Returns:
        JSON 标量或原始字符串。Any 仅用于 CLI 动态输入边界。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _print(data: dict[str, Any]) -> None:
    """以稳定、可读的 JSON 格式输出 RPC 结果。"""
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


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
        elif args.command == "read":
            result = await client.read(args.device_id, args.point_id)
        elif args.command == "tasks":
            result = await client.tasks()
        elif args.command == "task":
            result = await client.task(args.task_id)
        elif args.command == "task-instances":
            result = await client.task_instances()
        elif args.command == "task-instance":
            result = await client.task_instance(args.instance_id)
        elif args.command == "start":
            result = await client.start_task(args.task_id)
        elif args.command == "stop":
            result = await client.stop_task(args.task_id)
        elif args.command == "start-instance":
            result = await client.start_task_instance(args.instance_id)
        elif args.command == "stop-instance":
            result = await client.stop_task_instance(args.instance_id)
        elif args.command == "start-all":
            result = await client.start_all()
        elif args.command == "stop-all":
            result = await client.stop_all()
        elif args.command == "reload":
            result = await client.reload()
        elif args.command == "verify-device":
            result = await client.verify_device(
                args.device_id,
                timeout=args.probe_timeout,
            )
        elif args.command == "resolve-point":
            result = await client.resolve_point(args.device_id, args.point_id)
        elif args.command == "verify-point":
            result = await client.verify_point(args.device_id, args.point_id)
        elif args.command == "verify-points":
            result = await client.verify_points(
                args.device_id,
                point_group=args.group,
            )
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
    """构建 wind-hub-ctl 参数解析器。

    Returns:
        包含 Collector endpoint、RPC 超时和全部控制子命令的解析器。
    """
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

    sub.add_parser("tasks", help="列出 Task Definition 与聚合状态")

    task = sub.add_parser("task", help="按 task_id 查询一个 Task")
    task.add_argument("task_id")

    sub.add_parser("task-instances", help="列出全部 Task Instance")
    instance = sub.add_parser("task-instance", help="查询一个 Task Instance")
    instance.add_argument("instance_id")

    start = sub.add_parser("start", help="按 task_id 启动一个 Task")
    start.add_argument("task_id")

    stop = sub.add_parser("stop", help="按 task_id 停止一个 Task")
    stop.add_argument("task_id")

    start_instance = sub.add_parser("start-instance", help="启动一个 Task Instance")
    start_instance.add_argument("instance_id")

    stop_instance = sub.add_parser("stop-instance", help="停止一个 Task Instance")
    stop_instance.add_argument("instance_id")

    sub.add_parser("start-all", help="启动全部已分配 Task Instance")
    sub.add_parser("stop-all", help="停止全部已分配 Task Instance")
    sub.add_parser("reload", help="从 Collector 本地 YAML 执行增量热重载")

    verify_device = sub.add_parser("verify-device", help="验证设备通信链路")
    verify_device.add_argument("device_id")
    verify_device.add_argument(
        "--probe-timeout",
        type=float,
        default=1.0,
        help="ICMP/TCP 单阶段探测超时（秒）",
    )

    resolve_point = sub.add_parser("resolve-point", help="解析点位协议地址")
    resolve_point.add_argument("device_id")
    resolve_point.add_argument("point_id")

    verify_point = sub.add_parser("verify-point", help="实际读取并校验单个点位")
    verify_point.add_argument("device_id")
    verify_point.add_argument("point_id")

    verify_points = sub.add_parser("verify-points", help="批量校验设备点表或 point group")
    verify_points.add_argument("device_id")
    verify_points.add_argument(
        "--group",
        default=None,
        help="仅校验指定 point_group；省略时校验整个设备点表",
    )

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
