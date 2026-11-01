"""Generate the authoritative CLI v1 protobuf, gRPC, and Mypy contracts."""

from __future__ import annotations

from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
CONTRACTS_ROOT = SRC_ROOT / "contracts"

PROTO_FILES = (
    "contracts/common.proto",
    "contracts/collector.proto",
    "contracts/commander.proto",
    "contracts/sink_service.proto",
    "contracts/industrial_gateway.proto",
    "contracts/data_generator.proto",
)

# The old contract sources have been removed. Their generated files must not
# remain importable after successfully generating the authoritative definitions.
RETIRED_PROTO_NAMES = ("collector_v2", "sink", "gateway")
GENERATED_SUFFIXES = (
    "_pb2.py",
    "_pb2.pyi",
    "_pb2_grpc.py",
    "_pb2_grpc.pyi",
)


def main() -> int:
    """Generate all contracts; remove retired artifacts only after full success."""
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    options = [
        "grpc_tools.protoc",
        f"-I{SRC_ROOT}",
        f"-I{include_dir}",
        f"--python_out={SRC_ROOT}",
        f"--grpc_python_out={SRC_ROOT}",
        f"--mypy_out={SRC_ROOT}",
        f"--mypy_grpc_out={SRC_ROOT}",
        *(str(SRC_ROOT / name) for name in PROTO_FILES),
    ]
    result = int(protoc.main(options))
    if result != 0:
        return result

    for name in RETIRED_PROTO_NAMES:
        for suffix in GENERATED_SUFFIXES:
            (CONTRACTS_ROOT / f"{name}{suffix}").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
