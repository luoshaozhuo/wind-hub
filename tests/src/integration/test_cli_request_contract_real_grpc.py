"""CLI 请求级契约端到端测试（S3：write/batch/request-result 命令面）。

CLI 以独立子进程（``python -m cli.main``）启动，经临时 cli.yaml 接入
tests/src 共用的真实 Commander 栈——真实 mTLS 通道、磁盘 SQLite（WAL/FULL）
与真实 Modbus TCP 从站，不使用脚本化假服务器。子进程方式同时覆盖真实用户
入口：配置加载、TLS Profile 解析、Typer 命令解析与 JSON 信封输出。

覆盖任务书 S3 语义：
- write 在任何控制 RPC 前向 stderr 打印 request_id 回执，``--request-id``
  原样透传，受理回执携带同一 request_id；复用 ``--request-id`` 必须同时显式
  提供 ``--expires-at`` 与 ``--expected-revision``，缺任一项本地拒绝（exit 2）；
- 同 ``--request-id`` 同载荷二次写入幂等：command_id 不变，现场只写一次；
- 空白 ``--request-id`` 本地拒绝（exit 2），零 RPC、零现场写；
- batch 的 JSON/YAML 两种载体原子受理，同 ID 重放 command_id 序列不变；
- ``request-result`` 按 request_id 聚合回查；未知 ID 明确 NOT_FOUND（exit 1）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest

from contracts import commander_pb2 as pb
from tests.commander.grpc_harness import GrpcStack, Pki, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

_REPO_SRC = Path(__file__).resolve().parents[3] / "src"
_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED", "CANCELLED"}


@dataclass
class CliResult:
    exit_code: int
    stdout: str
    stderr: str

    def json(self) -> dict[str, Any]:
        lines = [line for line in self.stdout.splitlines() if line.startswith("{")]
        assert lines, f"no JSON envelope in output: {self.stdout!r}"
        return cast(dict[str, Any], json.loads(lines[-1]))


@dataclass
class CliStack:
    stack: GrpcStack
    pki: Pki
    config_path: Path
    modbus: ModbusMockServer

    async def run(self, *arguments: str) -> CliResult:
        # 子进程必须放到工作线程：阻塞式 subprocess.run 会卡住 pytest 事件循环，
        # 而同进程内的真实 gRPC 服务器依赖该循环处理 RPC（tests/cli 的假服务器
        # 跑在独立线程，不存在此约束）。
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "cli.main",
                "--config",
                str(self.config_path),
                "--format",
                "json",
                *arguments,
            ],
            cwd=_REPO_SRC,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return CliResult(completed.returncode, completed.stdout, completed.stderr)


def _write_cli_yaml(path: Path, pki: Pki, target: str) -> Path:
    services = "\n".join(
        f"""  {name}:
    grpc_endpoint: "{target}"
    tls_profile: mtls"""
        for name in (
            "collector",
            "commander",
            "sink_realtime",
            "sink_reliable",
            "gateway",
            "data_generator",
        )
    )
    path.write_text(
        f"""schema_version: 1
cli:
  timeout_seconds: 10
  output_format: json
services:
{services}

tls_profiles:
  mtls:
    ca_cert: {pki.tmp / "ca.crt"}
    client_cert: {pki.tmp / "ops.crt"}
    client_key: {pki.tmp / "ops.key"}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
async def cli_stack(
    tmp_path: Path, modbus_server: ModbusMockServer
) -> AsyncGenerator[CliStack, None]:
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-cli-contract",
        stream_prefix="CMD_SRC_CLI",
    )
    config_path = _write_cli_yaml(tmp_path / "cli.yaml", pki, stack.target)
    try:
        yield CliStack(stack, pki, config_path, modbus_server)
    finally:
        await server.stop(grace=5)
        await service.stop()


async def _revision(stack: GrpcStack, pki: Pki) -> str:
    _common, commander = stack.stubs(pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    return str(next(d.revision for d in devices.devices if d.device_id == "WT01"))


async def _poll_request_terminal(stack: GrpcStack, pki: Pki, request_id: str) -> Any:
    _common, commander = stack.stubs(pki, client="ops")
    for _ in range(200):
        try:
            view = await commander.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() is not grpc.StatusCode.NOT_FOUND:
                raise
        else:
            if view.state in _TERMINAL:
                return view
        await asyncio.sleep(0.05)
    raise AssertionError(f"request {request_id} never reached a terminal state")


async def test_cli_write_receipt_rpc_passthrough_and_request_result_roundtrip(
    cli_stack: CliStack,
) -> None:
    """write：回执先于 RPC 打印，--request-id 透传，request-result 聚合回查。"""
    request_id = f"req-cli-write-{uuid4().hex}"
    revision = await _revision(cli_stack.stack, cli_stack.pki)
    expiry = (datetime.now(UTC) + timedelta(seconds=600)).isoformat()
    result = await cli_stack.run(
        "commander",
        "write",
        "WT01",
        "setpoint_power",
        "42.5",
        "--yes",
        "--request-id",
        request_id,
        "--expires-at",
        expiry,
        "--expected-revision",
        revision,
    )
    assert result.exit_code == 0, result.stdout
    # 回执在 stderr，且必须出现在进程退出前（实现上先于任何控制 RPC 发送）。
    assert f"Commander request_id={request_id}" in result.stderr
    data = result.json()["data"]
    assert data["accepted"] is True
    assert data["request_id"] == request_id
    assert data["command_id"]

    view = await _poll_request_terminal(cli_stack.stack, cli_stack.pki, request_id)
    assert view.state == "SUCCEEDED"
    assert view.io_settled is True

    # request-result 子命令走独立 GetRequestResult RPC 拿回同一聚合视图。
    query = await cli_stack.run("commander", "request-result", request_id)
    assert query.exit_code == 0, query.stdout
    payload = query.json()["data"]
    assert payload["request_id"] == request_id
    assert [item["command_id"] for item in payload["commands"]] == [data["command_id"]]
    assert payload["state"] == "SUCCEEDED"


async def test_cli_write_replayed_request_id_dedupes_with_single_field_write(
    cli_stack: CliStack,
) -> None:
    """同 --request-id 同载荷二次写入：command_id 不变，Modbus 从站只写一次。

    两次调用必须固定 ``--expires-at``：CLI 默认按调用时刻生成期限，不固定
    期限会让请求指纹天然不同而被 ALREADY_EXISTS 拒绝（这本身是正确行为，
    由组件层指纹测试覆盖）。
    """
    request_id = f"req-cli-replay-{uuid4().hex}"
    expiry = (datetime.now(UTC) + timedelta(seconds=600)).isoformat()
    revision = await _revision(cli_stack.stack, cli_stack.pki)
    before = cli_stack.modbus.write_count

    first = await cli_stack.run(
        "commander",
        "write",
        "WT01",
        "setpoint_power",
        "7.5",
        "--yes",
        "--request-id",
        request_id,
        "--expires-at",
        expiry,
        "--expected-revision",
        revision,
    )
    assert first.exit_code == 0, first.stdout
    first_command_id = first.json()["data"]["command_id"]
    await _poll_request_terminal(cli_stack.stack, cli_stack.pki, request_id)
    assert cli_stack.modbus.write_count - before == 1

    second = await cli_stack.run(
        "commander",
        "write",
        "WT01",
        "setpoint_power",
        "7.5",
        "--yes",
        "--request-id",
        request_id,
        "--expires-at",
        expiry,
        "--expected-revision",
        revision,
    )
    assert second.exit_code == 0, second.stdout
    # 持久层幂等回执：同一 command_id，不产生第二次现场写。
    assert second.json()["data"]["command_id"] == first_command_id
    assert cli_stack.modbus.write_count - before == 1


async def test_cli_blank_request_id_rejected_locally_without_rpc(
    cli_stack: CliStack,
) -> None:
    """空白 --request-id 在 CLI 本地拒绝（exit 2）：无受理、无现场写。"""
    before = cli_stack.modbus.write_count
    result = await cli_stack.run(
        "commander",
        "write",
        "WT01",
        "setpoint_power",
        "1.0",
        "--yes",
        "--request-id",
        "   ",
    )
    assert result.exit_code == 2
    assert "nonempty" in result.stdout
    assert cli_stack.modbus.write_count == before


@pytest.mark.parametrize("suffix", ["json", "yaml"])
async def test_cli_batch_json_and_yaml_replay_is_idempotent(
    cli_stack: CliStack, suffix: str
) -> None:
    """batch：JSON/YAML 两载体原子受理；同 request_id 重放零新增现场写。"""
    revision = await _revision(cli_stack.stack, cli_stack.pki)
    request_id = f"req-cli-batch-{suffix}-{uuid4().hex}"
    commands = [
        {
            "device_id": "WT01",
            "point_id": "setpoint_power",
            "value": 40.5,
            "expected_revision": revision,
        },
        {
            "device_id": "WT01",
            "point_id": "setpoint_power",
            "value": 41.5,
            "expected_revision": revision,
        },
    ]
    batch_path = cli_stack.config_path.parent / f"batch.{suffix}"
    if suffix == "json":
        batch_path.write_text(json.dumps({"commands": commands}), encoding="utf-8")
    else:
        yaml_text = "commands:\n" + "".join(
            f"- device_id: {item['device_id']}\n"
            f"  point_id: {item['point_id']}\n"
            f"  value: {item['value']}\n"
            f"  expected_revision: {item['expected_revision']}\n"
            for item in commands
        )
        batch_path.write_text(yaml_text, encoding="utf-8")

    before = cli_stack.modbus.write_count
    first = await cli_stack.run(
        "commander", "batch", str(batch_path), "--yes", "--request-id", request_id
    )
    assert first.exit_code == 0, first.stdout
    assert f"Commander request_id={request_id}" in first.stderr
    first_ids = list(first.json()["data"]["command_ids"])
    assert len(first_ids) == 2 and len(set(first_ids)) == 2
    await _poll_request_terminal(cli_stack.stack, cli_stack.pki, request_id)
    assert cli_stack.modbus.write_count - before == 2

    # 整批同 ID 同指纹重放：回执序列一致，现场写不增。
    replay = await cli_stack.run(
        "commander", "batch", str(batch_path), "--yes", "--request-id", request_id
    )
    assert replay.exit_code == 0, replay.stdout
    assert list(replay.json()["data"]["command_ids"]) == first_ids
    assert cli_stack.modbus.write_count - before == 2


async def test_cli_request_result_unknown_id_reports_not_found(cli_stack: CliStack) -> None:
    """未知 request_id：服务端 NOT_FOUND 映射为业务失败 exit 1，不返回空投影。"""
    result = await cli_stack.run("commander", "request-result", f"req-cli-missing-{uuid4().hex}")
    assert result.exit_code == 1
    assert "NOT_FOUND" in result.stdout
