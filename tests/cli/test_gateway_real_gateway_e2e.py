"""R1 P1-02：真实 CLI 子进程 × 真实生产 Gateway gRPC/mTLS 系统测试。

与 tests/cli/test_gateway_e2e.py 的 FakeRpcServer 互补：本文件对端是生产
``GatewayGrpcService`` + ``GatewayRuntime``（start_gateway_v1 不替换生产对象），
CLI 以独立解释器子进程（``python -m cli.main``）运行，验证：

- 帮助/参数解析与 CLI v1_26 冻结接口一致（位置参数 ID=JSON_VALUE、必填
  --owner-id、--lease-ms 边界、无 --values）；
- 有权限时 ClaimTargets(ACQUIRE) → UpdateVariables → ClaimTargets(RELEASE)
  完整闭环并真实落库，释放后目标可被再次 ACQUIRE；
- 无 inject 权限、租约冲突、未知 Gateway 均无非法写入；
- 本地参数错误在任何 RPC 之前退出 2。

UNKNOWN/结果不完整/RELEASE 失败等 CLI 内部语义由 FakeRpcServer 套件覆盖。
注意：生产 grpc.aio 服务器与本测试共用事件循环，因此子进程必须异步等待，
阻塞式 subprocess.run 会饿死服务器循环并造成假性 DEADLINE_EXCEEDED。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as pb_rpc
from contracts.scalar_codec import decode_scalar
from tests.cli.e2e_util import SRC, last_json, write_cli_yaml
from tests.support.gateway_v1 import (
    FEEDBACK_FLOAT,
    FEEDBACK_UINT,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [pytest.mark.system, pytest.mark.contract, pytest.mark.real_service]

INJECTOR = "cli-injector"
HOLDER = "cli-holder"
READER = "cli-reader"
ADMIN = "gw-admin"

RIGHTS: dict[str, list[str]] = {
    INJECTOR: ["inject", "read"],
    HOLDER: ["inject", "read"],
    READER: ["read"],
    ADMIN: ["read", "inject", "lifecycle", "diagnose", "config_read", "config_write"],
}


@dataclass
class CliResult:
    returncode: int | None
    stdout: str
    stderr: str


@pytest.fixture
async def stack(tmp_path: Path):
    harness = await start_gateway_v1(
        tmp_path / "root",
        two_server_specs(control_enabled=False),
        mtls=RIGHTS,
        bootstrap_cn=ADMIN,
    )
    try:
        yield harness, tmp_path
    finally:
        await harness.stop()


def _cli_config(stack, tmp_path: Path, cn: str) -> Path:
    harness, _ = stack
    pki = harness.pki
    assert pki is not None
    cert_path, key_path = pki["clients"][cn]  # type: ignore[index]
    cert_files = {
        "ca": pki["ca"],  # type: ignore[index]
        "client_crt": cert_path,
        "client_key": key_path,
    }
    return write_cli_yaml(
        tmp_path / f"cli-{cn}.yaml",
        cert_files,  # type: ignore[arg-type]
        {"gateway": f"localhost:{harness.grpc_port}"},
    )


async def _run_cli(config: Path, *arguments: str) -> CliResult:
    """真实独立 CLI 子进程；异步等待以保持生产 grpc.aio 服务器事件循环运转。"""
    args = [
        sys.executable,
        "-m",
        "cli.main",
        "--config",
        str(config),
        "--format",
        "json",
        *arguments,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=SRC,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return CliResult(proc.returncode, stdout.decode(), stderr.decode())


async def _read_value(stack, variable_id: str) -> Any:
    harness, _ = stack
    reply = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[variable_id])
    )
    sample = reply.values[0]
    if sample.quality == "UNINITIALIZED":
        return None
    return decode_scalar(sample.value)


async def test_help_matches_frozen_interface_without_values_option(stack) -> None:
    _, tmp_path = stack
    config = _cli_config(stack, tmp_path, INJECTOR)
    result = await _run_cli(config, "gateway", "update", "--help")
    assert result.returncode == 0, result.stdout
    text = result.stdout
    assert "--owner-id" in text
    assert "variable_id=typed_value" in text
    # v1_26 已删除 --values PATH 替代接口：任何残留都视为契约回归。
    assert "--values" not in text


async def test_happy_path_claim_update_release_against_real_gateway(stack) -> None:
    _, tmp_path = stack
    config = _cli_config(stack, tmp_path, INJECTOR)

    result = await _run_cli(
        config,
        "gateway",
        "update",
        "gw1",
        f"{FEEDBACK_UINT}=42",
        f"{FEEDBACK_FLOAT}=1.5",
        "--owner-id",
        "cli-owner-a",
        "--lease-ms",
        "10000",
        "--yes",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    envelope = last_json(result.stdout)
    assert envelope["status"] in {"OK", "SUCCESS"}
    assert len(envelope["data"]["results"]) == 2

    assert await _read_value(stack, FEEDBACK_UINT) == 42
    assert await _read_value(stack, FEEDBACK_FLOAT) == pytest.approx(1.5)

    # RELEASE 已真实发生：同一目标可被其他主体立即 ACQUIRE。
    harness, _ = stack
    channel = harness.mtls_channel(HOLDER)
    try:
        stub = pb_rpc.IndustrialGatewayServiceStub(channel)
        again = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id="cli-owner-b",
                variable_ids=[FEEDBACK_UINT, FEEDBACK_FLOAT],
                action="ACQUIRE",
                lease_ms=10000,
            )
        )
        assert again.accepted, again.error_code
        release = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id="cli-owner-b",
                variable_ids=[FEEDBACK_UINT, FEEDBACK_FLOAT],
                action="RELEASE",
                lease_token=again.lease_token,
            )
        )
        assert release.accepted
    finally:
        await channel.close()


@pytest.mark.parametrize(
    "arguments",
    [
        ["gw1", f"{FEEDBACK_UINT}=1", f"{FEEDBACK_UINT}=2", "--owner-id", "o", "--yes"],
        ["gw1", f"{FEEDBACK_UINT}=not-json", "--owner-id", "o", "--yes"],
        ["gw1", f"{FEEDBACK_UINT}=1", "--owner-id", "  ", "--yes"],
        ["gw1", f"{FEEDBACK_UINT}=1", "--yes"],
        ["gw1", f"{FEEDBACK_UINT}=1", "--owner-id", "o", "--lease-ms", "500", "--yes"],
        ["gw1", f"{FEEDBACK_UINT}=1", "--owner-id", "o", "--lease-ms", "70000", "--yes"],
        ["gw1", f"{FEEDBACK_UINT}=1", "--owner-id", "o", "--values", "x.json", "--yes"],
    ],
)
async def test_local_argument_rejections_exit_2_without_write(stack, arguments) -> None:
    _, tmp_path = stack
    config = _cli_config(stack, tmp_path, INJECTOR)
    result = await _run_cli(config, "gateway", "update", *arguments)
    assert result.returncode == 2, (arguments, result.stdout, result.stderr)
    assert await _read_value(stack, FEEDBACK_UINT) is None


async def test_reader_certificate_is_permission_denied_without_write(stack) -> None:
    _, tmp_path = stack
    config = _cli_config(stack, tmp_path, READER)
    result = await _run_cli(
        config,
        "gateway",
        "update",
        "gw1",
        f"{FEEDBACK_UINT}=9",
        "--owner-id",
        "reader-owner",
        "--yes",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    envelope = last_json(result.stdout)
    assert envelope["status"] in {"ERROR", "FAILED", "FAILURE"}
    assert await _read_value(stack, FEEDBACK_UINT) is None


async def test_lease_conflict_denies_cli_without_write(stack) -> None:
    harness, tmp_path = stack
    channel = harness.mtls_channel(HOLDER)
    try:
        holder = pb_rpc.IndustrialGatewayServiceStub(channel)
        claimed = await holder.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id="holder-owner",
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=30000,
            )
        )
        assert claimed.accepted, claimed.error_code

        config = _cli_config(stack, tmp_path, INJECTOR)
        result = await _run_cli(
            config,
            "gateway",
            "update",
            "gw1",
            f"{FEEDBACK_UINT}=33",
            "--owner-id",
            "cli-owner-a",
            "--yes",
        )
        # 租约被占：ClaimTargets 业务拒绝，CLI 退出 2，且不发出 UpdateVariables。
        assert result.returncode == 2, result.stdout + result.stderr
        assert "TARGET_CLAIMED" in result.stdout
        assert await _read_value(stack, FEEDBACK_UINT) is None

        release = await holder.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id="holder-owner",
                variable_ids=[FEEDBACK_UINT],
                action="RELEASE",
                lease_token=claimed.lease_token,
            )
        )
        assert release.accepted
    finally:
        await channel.close()


async def test_unknown_gateway_id_is_business_failure_exit_1(stack) -> None:
    _, tmp_path = stack
    config = _cli_config(stack, tmp_path, INJECTOR)
    result = await _run_cli(
        config,
        "gateway",
        "update",
        "gw-does-not-exist",
        f"{FEEDBACK_UINT}=1",
        "--owner-id",
        "cli-owner-a",
        "--yes",
    )
    assert result.returncode == 1, result.stdout + result.stderr
