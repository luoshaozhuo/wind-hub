"""重新生成 Collector、Commander 各自的 Protobuf/gRPC Python 代码。"""

from __future__ import annotations

from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
PROTO_FILES = (
    SRC_ROOT / "collector" / "infrastructure" / "grpc" / "collector.proto",
    SRC_ROOT / "commander" / "infrastructure" / "grpc" / "commander.proto",
)


def main() -> int:
    """使用项目内 proto 生成 pb2、gRPC stub 及 mypy stub。"""
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    for proto in PROTO_FILES:
        result = protoc.main(
            [
                "grpc_tools.protoc",
                f"-I{SRC_ROOT}",
                f"-I{include_dir}",
                f"--python_out={SRC_ROOT}",
                f"--grpc_python_out={SRC_ROOT}",
                f"--mypy_out={SRC_ROOT}",
                f"--mypy_grpc_out={SRC_ROOT}",
                str(proto),
            ]
        )
        if result != 0:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
