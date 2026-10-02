"""生成 wind-hub-core 中提交入库的 gRPC/Protobuf Python 代码。"""

from __future__ import annotations

from pathlib import Path

import grpc_tools
from grpc_tools import protoc


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT / "src" / "wind-hub-core"
RPC_DIR = CORE_ROOT / "wind_hub_core" / "rpc"


def main() -> int:
    """从共享 proto 源重新生成 Commander/Collector pb2 与 gRPC stub。"""
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    protos = (
        RPC_DIR / "commander.proto",
        RPC_DIR / "collector.proto",
    )
    return protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{CORE_ROOT}",
            f"-I{include_dir}",
            f"--python_out={CORE_ROOT}",
            f"--grpc_python_out={CORE_ROOT}",
            *(str(proto) for proto in protos),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
