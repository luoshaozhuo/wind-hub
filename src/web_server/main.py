"""Web Server 进程入口及运行参数。"""

from __future__ import annotations

import os

import uvicorn

from web_server.app import create_app
from web_server.rpc import RpcSettings


def main() -> None:
    """通过环境变量配置监听地址及 Collector/Commander RPC 端点。"""
    settings = RpcSettings(
        collector_target=os.getenv("WIND_HUB_COLLECTOR_RPC", "127.0.0.1:50051"),
        commander_target=os.getenv("WIND_HUB_COMMANDER_RPC", "127.0.0.1:50052"),
        timeout_seconds=float(os.getenv("WIND_HUB_WEB_RPC_TIMEOUT", "5")),
    )
    uvicorn.run(
        create_app(settings, write_token=os.getenv("WIND_HUB_WEB_WRITE_TOKEN")),
        host=os.getenv("WIND_HUB_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WIND_HUB_WEB_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
