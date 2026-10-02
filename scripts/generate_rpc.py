"""生成 wind_hub_core 中提交入库的 gRPC/Protobuf Python 代码。"""

from __future__ import annotations

from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
RPC_DIR = SRC_ROOT / "wind_hub_core" / "rpc"


def main() -> int:
    """从共享 proto 源重新生成 Commander/Collector pb2、gRPC stub 与 mypy stub。

    ``--mypy_out`` / ``--mypy_grpc_out`` 由 mypy-protobuf 提供（dev 依赖），
    生成配套的 ``*_pb2.pyi`` / ``*_pb2_grpc.pyi``——mypy strict 对 RPC
    边界做类型检查依赖这些 stub。
    """
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    protos = (
        RPC_DIR / "commander.proto",
        RPC_DIR / "collector.proto",
    )
    return protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{SRC_ROOT}",
            f"-I{include_dir}",
            f"--python_out={SRC_ROOT}",
            f"--grpc_python_out={SRC_ROOT}",
            f"--mypy_out={SRC_ROOT}",
            f"--mypy_grpc_out={SRC_ROOT}",
            *(str(proto) for proto in protos),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
