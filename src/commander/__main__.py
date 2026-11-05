"""Commander gRPC service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from commander.infrastructure.assembly import build_commander
from commander.infrastructure.grpc_transport import run_grpc
from core.application.protocol_registry import ProtocolRegistry
from core.infrastructure.protocol import ADSDriver, IEC104Driver, ModbusDriver


def main() -> None:
    parser = argparse.ArgumentParser(description="wind-hub Commander gRPC service")
    parser.add_argument(
        "--config", required=True, type=Path, help="Commander service YAML pathname"
    )
    args = parser.parse_args()
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("modbus", ModbusDriver)
    registry.register("iec104", IEC104Driver)
    service = build_commander(service_config=args.config, protocol_registry=registry)
    asyncio.run(run_grpc(service, args.config))


if __name__ == "__main__":
    main()
