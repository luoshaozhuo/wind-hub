"""生成 wind_hub_core 中提交入库的 gRPC/Protobuf Python 代码。"""

from __future__ import annotations

from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
RPC_DIR = SRC_ROOT / "wind_hub_core" / "rpc"

#: 进程内 proto 副本（新栈）与旧共享 proto（wind_hub_core/rpc）的对应关系。
#: 两侧 proto 逐字节一致，生成的 message 全限定名相同；protobuf 默认
#: descriptor pool 不允许同名符号注册两次，因此新栈 pb2 的
#: FileDescriptorProto.name 必须与旧栈一致（同名同内容重复注册是
#: 幂等安全的），否则同一 Python 进程无法同时 import 新旧 pb2
#: （保留的 Server/ctl 走旧 pb2，新 Collector/Commander 走新 pb2）。
COPIED_PROTOS = (
    (
        SRC_ROOT / "collector" / "infrastructure" / "grpc" / "collector.proto",
        "collector/infrastructure/grpc/collector.proto",
        "wind_hub_core/rpc/collector.proto",
    ),
    (
        SRC_ROOT / "commander" / "infrastructure" / "grpc" / "commander.proto",
        "commander/infrastructure/grpc/commander.proto",
        "wind_hub_core/rpc/commander.proto",
    ),
)


def _align_descriptor_name(pb2_path: Path, generated_name: str, canonical_name: str) -> None:
    """把生成 pb2 的描述文件名替换为规范名（含长度前缀同步修正）。"""
    source = pb2_path.read_text(encoding="utf-8")
    # 描述名以 ``\\nX<name>`` 出现在序列化字面量开头，X 为长度前缀字符。
    bad = f"\\n{chr(len(generated_name))}{generated_name}"
    good = f"\\n{chr(len(canonical_name))}{canonical_name}"
    if bad not in source:
        raise RuntimeError(f"descriptor name not found in {pb2_path}")
    pb2_path.write_text(source.replace(bad, good, 1), encoding="utf-8")


def main() -> int:
    """从共享 proto 源重新生成 Commander/Collector pb2、gRPC stub 与 mypy stub。

    ``--mypy_out`` / ``--mypy_grpc_out`` 由 mypy-protobuf 提供（dev 依赖），
    生成配套的 ``*_pb2.pyi`` / ``*_pb2_grpc.pyi``——mypy strict 对 RPC
    边界做类型检查依赖这些 stub。

    两遍生成：先旧共享包（wind_hub_core/rpc），再进程内副本
    （collector/commander 各自 infrastructure/grpc）；副本生成后把描述
    文件名对齐为旧共享包规范名，保证同进程共存。
    """
    include_dir = Path(grpc_tools.__file__).resolve().parent / "_proto"
    # 两侧 proto 符号同名，必须分次调用 protoc（单次调用拒绝重复定义）。
    passes = (
        (RPC_DIR / "commander.proto", RPC_DIR / "collector.proto"),
        tuple(proto for proto, _, _ in COPIED_PROTOS),
    )
    for protos in passes:
        result = protoc.main(
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
        if result != 0:
            return result
    for proto, generated_name, canonical_name in COPIED_PROTOS:
        _align_descriptor_name(
            proto.with_name(f"{proto.stem}_pb2.py"), generated_name, canonical_name
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
