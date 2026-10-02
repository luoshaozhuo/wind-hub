"""wind-hub-server 命令行入口。

仅解析进程宿主参数并调用 :func:`wind_hub_server.server.run_server`；
业务配置继续由 wind-hub YAML 配置体系管理。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from wind_hub_server.server import run_server
from wind_hub_server.settings import ServerSettings


def build_parser() -> argparse.ArgumentParser:
    """构建 ``wind-hub-server`` 参数解析器。

    Returns:
        独立的 argparse 解析器，便于单元测试和嵌入式调用。
    """
    parser = argparse.ArgumentParser(
        prog="wind-hub-server",
        description="启动 wind-hub 管理 API 与通信 Runtime。",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="现场配置目录（<site>/；公共定义可位于同级 common/）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API 监听地址")
    parser.add_argument("--port", type=int, default=8080, help="API 监听端口")
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=30.0,
        help="Runtime 优雅停机硬超时（秒）",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="uvicorn 日志级别（debug/info/warning/error/critical）",
    )
    parser.add_argument(
        "--collector-target",
        default="127.0.0.1:50051",
        help="Collector gRPC endpoint，默认 127.0.0.1:50051",
    )
    parser.add_argument(
        "--commander-target",
        default="127.0.0.1:50052",
        help="Commander gRPC endpoint，默认 127.0.0.1:50052",
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=30.0,
        help="Worker 配置 revision/hash 对账周期（秒），默认 30",
    )
    parser.add_argument(
        "--worker-probe-interval",
        type=float,
        default=5.0,
        help="Worker 状态探测周期（秒），默认 5",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并启动服务进程。

    Args:
        argv: 可选参数序列；``None`` 时读取当前进程 argv。

    Returns:
        服务进程退出码。
    """
    args = build_parser().parse_args(argv)
    settings = ServerSettings.from_values(
        args.config,
        host=args.host,
        port=args.port,
        shutdown_timeout=args.shutdown_timeout,
        log_level=args.log_level,
        collector_target=args.collector_target,
        commander_target=args.commander_target,
        reconcile_interval=args.reconcile_interval,
        worker_probe_interval=args.worker_probe_interval,
    )
    return asyncio.run(run_server(settings))


if __name__ == "__main__":
    raise SystemExit(main())
