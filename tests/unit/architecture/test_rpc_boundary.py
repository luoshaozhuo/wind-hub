"""全仓 RPC 边界与命名收尾约束（Phase 5）。

证明：
- generated protobuf（``*_pb2`` / ``*_pb2_grpc``）只出现在允许的边界区域；
- application/domain 不直接引用 protobuf 生成物；
- 旧 ``application.usecase`` 容器与旧 ``*UseCase`` 类名在全仓生产代码无残留。

扫描基于源码文本与前缀白名单，不绑定精确文件数量。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"

# generated protobuf 允许出现的前缀（相对 src/）：
# grpc inbound/outbound adapter、core rpc codec 与生成物自身、ctl RPC client。
_ALLOWED_PB2_PREFIXES = (
    "wind_hub_collector/adapter/inbound/grpc/",
    "wind_hub_commander/adapter/inbound/grpc/",
    "wind_hub_server/adapter/outbound/grpc/",
    "wind_hub_core/rpc/",
    "wind_hub_ctl/",
    "collector/infrastructure/grpc/",
    "commander/infrastructure/grpc/",
)

# application/domain 一律不感知 wire 形态。
_NO_PB2_PREFIXES = (
    "wind_hub_collector/application/",
    "wind_hub_collector/domain/",
    "wind_hub_commander/application/",
    "wind_hub_server/application/",
)

_LEGACY_USECASE_NAMES = (
    "QueryUseCase",
    "TaskUseCase",
    "ConfigUseCase",
    "CommandUseCase",
    "ReadUseCase",
    "DiagnosticUseCase",
)


def _source_files() -> list[Path]:
    return [
        path
        for path in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and "_pb2" not in path.name
    ]


def _relative(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def test_pb2_only_in_allowed_zones() -> None:
    """引用 pb2 生成物的文件全部位于 adapter/codec/ctl 边界内。"""
    offenders = [
        _relative(path)
        for path in _source_files()
        if "_pb2" in path.read_text(encoding="utf-8")
        and not _relative(path).startswith(_ALLOWED_PB2_PREFIXES)
    ]
    assert offenders == []


def test_application_and_domain_free_of_pb2() -> None:
    """application/domain 源码不出现 pb2 引用（包括注释与字符串）。"""
    offenders = [
        _relative(path)
        for path in _source_files()
        if _relative(path).startswith(_NO_PB2_PREFIXES)
        and "_pb2" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_application_usecase_imports() -> None:
    """旧 application.usecase 容器路径在全仓生产代码无残留。"""
    offenders = [
        _relative(path)
        for path in _source_files()
        if "application.usecase" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_legacy_usecase_class_names() -> None:
    """泛化 *UseCase 旧类名在全仓生产代码无残留。"""
    offenders = [
        (_relative(path), name)
        for path in _source_files()
        for name in _LEGACY_USECASE_NAMES
        # 词边界匹配：DiffConfigUseCase 等带语义前缀的用例名不算旧名残留。
        if re.search(rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
