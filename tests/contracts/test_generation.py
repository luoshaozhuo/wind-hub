"""Proto 生成结果：产物完整、可导入、可重复、旧产物已清理。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from contracts import (
    collector_pb2,
    collector_pb2_grpc,
    commander_pb2,
    commander_pb2_grpc,
    common_pb2,
    common_pb2_grpc,
    data_generator_pb2,
    data_generator_pb2_grpc,
    industrial_gateway_pb2,
    industrial_gateway_pb2_grpc,
    sink_service_pb2,
    sink_service_pb2_grpc,
)

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = ROOT / "src" / "contracts"

PROTO_STEMS = (
    "common",
    "collector",
    "commander",
    "sink_service",
    "industrial_gateway",
    "data_generator",
)
GENERATED_SUFFIXES = ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py", "_pb2_grpc.pyi")
RETIRED_STEMS = ("collector_v2", "sink", "gateway")


def _generated_paths() -> set[Path]:
    return {
        CONTRACTS_ROOT / f"{stem}{suffix}" for stem in PROTO_STEMS for suffix in GENERATED_SUFFIXES
    }


def test_all_generated_artifacts_exist() -> None:
    missing = [str(p) for p in _generated_paths() if not p.is_file()]
    assert missing == []


def test_all_generated_modules_importable() -> None:
    # 导入成功即完成 pb2 与 grpc stub 的可导入校验；模块对象在本模块顶部绑定。
    modules = (
        common_pb2,
        collector_pb2,
        commander_pb2,
        sink_service_pb2,
        industrial_gateway_pb2,
        data_generator_pb2,
        common_pb2_grpc,
        collector_pb2_grpc,
        commander_pb2_grpc,
        sink_service_pb2_grpc,
        industrial_gateway_pb2_grpc,
        data_generator_pb2_grpc,
    )
    assert len(modules) == 12
    for module in modules:
        assert module.__name__.startswith("contracts.")


@pytest.mark.parametrize(
    ("grpc_module", "servicer", "registration"),
    [
        (common_pb2_grpc, "CommonServiceServicer", "add_CommonServiceServicer_to_server"),
        (collector_pb2_grpc, "CollectorServiceServicer", "add_CollectorServiceServicer_to_server"),
        (commander_pb2_grpc, "CommanderServiceServicer", "add_CommanderServiceServicer_to_server"),
        (sink_service_pb2_grpc, "SinkServiceServicer", "add_SinkServiceServicer_to_server"),
        (
            industrial_gateway_pb2_grpc,
            "IndustrialGatewayServiceServicer",
            "add_IndustrialGatewayServiceServicer_to_server",
        ),
        (
            data_generator_pb2_grpc,
            "DataGeneratorServiceServicer",
            "add_DataGeneratorServiceServicer_to_server",
        ),
    ],
)
def test_servicer_and_registration_present(
    grpc_module: object, servicer: str, registration: str
) -> None:
    assert hasattr(grpc_module, servicer)
    assert callable(getattr(grpc_module, registration))


def test_generation_is_byte_reproducible() -> None:
    def digests() -> dict[Path, str]:
        return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in _generated_paths()}

    before = digests()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_rpc.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert digests() == before


def test_retired_artifacts_absent() -> None:
    leftovers = [
        str(CONTRACTS_ROOT / f"{stem}{suffix}")
        for stem in RETIRED_STEMS
        for suffix in GENERATED_SUFFIXES
        if (CONTRACTS_ROOT / f"{stem}{suffix}").exists()
    ]
    assert leftovers == []


def test_no_unexpected_generated_modules() -> None:
    actual = {
        path.name for path in CONTRACTS_ROOT.glob("*_pb2*.py*") if not path.name.endswith(".pyc")
    }
    expected = {path.name for path in _generated_paths()}
    assert actual == expected
