"""统一生成公开 RPC 契约（Python/gRPC 与 Mypy stub）。"""

from __future__ import annotations

from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
PROTO_FILES = (
    "contracts/common.proto",
    "contracts/collector_v2.proto",
    "contracts/sink.proto",
    "contracts/commander.proto",
    "contracts/gateway.proto",
)


def main() -> int:
    """单个 proto 出错时立即失败，避免生成文件悄然陈旧。"""
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    for name in PROTO_FILES:
        options = [
            "grpc_tools.protoc",
            f"-I{SRC_ROOT}",
            f"-I{include_dir}",
            f"--python_out={SRC_ROOT}",
            f"--mypy_out={SRC_ROOT}",
        ]
        if name != "contracts/common.proto":
            options.extend((f"--grpc_python_out={SRC_ROOT}", f"--mypy_grpc_out={SRC_ROOT}"))
        if result := protoc.main([*options, str(SRC_ROOT / name)]):
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
