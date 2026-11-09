"""Generate or verify authoritative protobuf, gRPC, and Mypy bindings."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

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

RETIRED_PROTO_NAMES = ("collector_v2", "sink", "gateway")
GENERATED_SUFFIXES = (
    "_pb2.py",
    "_pb2.pyi",
    "_pb2_grpc.py",
    "_pb2_grpc.pyi",
)


def _compile(output_root: Path) -> int:
    """Generate every wire binding in the requested destination."""
    (output_root / "contracts").mkdir(parents=True, exist_ok=True)
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    options = [
        "grpc_tools.protoc",
        f"-I{SRC_ROOT}",
        f"-I{include_dir}",
        f"--python_out={output_root}",
        f"--grpc_python_out={output_root}",
        f"--mypy_out={output_root}",
        f"--mypy_grpc_out={output_root}",
        *(str(SRC_ROOT / name) for name in PROTO_FILES),
    ]
    return int(protoc.main(options))


def _check_generated() -> int:
    """Verify checked-in bindings without modifying repository files."""
    with TemporaryDirectory(prefix="wind-hub-proto-check-") as directory:
        output_root = Path(directory)
        result = _compile(output_root)
        if result:
            return result

        mismatches: list[str] = []
        for proto in PROTO_FILES:
            stem = Path(proto).stem
            for suffix in GENERATED_SUFFIXES:
                relative = Path("contracts") / f"{stem}{suffix}"
                expected = output_root / relative
                actual = SRC_ROOT / relative
                if not expected.is_file() or not actual.is_file():
                    mismatches.append(f"{relative}: missing generated binding")
                elif expected.read_bytes() != actual.read_bytes():
                    mismatches.append(f"{relative}: stale generated binding")
        for name in RETIRED_PROTO_NAMES:
            for suffix in GENERATED_SUFFIXES:
                retired = CONTRACTS_ROOT / f"{name}{suffix}"
                if retired.exists():
                    mismatches.append(f"{retired.relative_to(SRC_ROOT)}: retired binding")
        for mismatch in mismatches:
            print(mismatch, file=sys.stderr)
        return 1 if mismatches else 0


def main() -> int:
    """Generate bindings or perform a side-effect-free equality check."""
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        return _check_generated()
    if len(sys.argv) != 1:
        print("usage: python scripts/generate_rpc.py [--check]", file=sys.stderr)
        return 2

    result = _compile(SRC_ROOT)
    if result:
        return result

    for name in RETIRED_PROTO_NAMES:
        for suffix in GENERATED_SUFFIXES:
            (CONTRACTS_ROOT / f"{name}{suffix}").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
