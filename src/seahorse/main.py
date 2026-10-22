"""Seahorse 独立命令行入口。"""
from __future__ import annotations

import argparse
from pathlib import Path

from seahorse.infrastructure.config import load_config
from seahorse.infrastructure.factory import create_runtime
from seahorse.infrastructure.modbus_publisher import GatewayModbusPublisher, load_publish_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="seahorse")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--gateway-map", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    publisher = (
        GatewayModbusPublisher(load_publish_config(args.gateway_map))
        if args.gateway_map is not None
        else None
    )
    if not args.check:
        create_runtime(config, publisher=publisher).run(steps=args.steps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
