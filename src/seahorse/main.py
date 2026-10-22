"""Seahorse 独立命令行入口。"""
from __future__ import annotations

import argparse
from pathlib import Path

from seahorse.infrastructure.config import load_config
from seahorse.infrastructure.factory import create_runtime


def main() -> int:
    parser = argparse.ArgumentParser(prog="seahorse")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--steps", type=int)
    args = parser.parse_args()
    config = load_config(args.config)
    if not args.check:
        create_runtime(config).run(steps=args.steps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
