"""生成 wind-hub-core 中提交入库的 gRPC/Protobuf Python 代码。"""

from __future__ import annotations

from pathlib import Path

from grpc_tools import protoc
import grpc_tools


ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = ROOT / "src" / "wind-hub-core" / "wind_hub_core" / "rpc" / "proto"
OUT_DIR = ROOT / "src" / "wind-hub-core" / "wind_hub_core" / "rpc"


def main() -> int:
    """从共享 proto 源重新生成 pb2/pb2_grpc 文件。"""
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    proto = RPC_DIR / "commander.proto"
    return protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{ROOT / 'src' / 'wind-hub-core'}",
            f"-I{include_dir}",
            f"--python_out={ROOT / 'src' / 'wind-hub-core'}",
            f"--grpc_python_out={ROOT / 'src' / 'wind-hub-core'}",
            str(proto),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
