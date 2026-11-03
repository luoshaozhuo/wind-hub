"""真实 grpc.aio + mTLS 端到端：连接复用、错误码映射、写入安全、轮询、快照（冻结契约）。

服务器是真实 grpc.aio（TLS 握手、protobuf 序列化、gRPC status 均为真实实现），
仅业务行为由测试注入；这覆盖 CLI 侧契约，不替代五服务生产服务端验收。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import grpc
import pytest

from cli.runtime import Runtime
from cli.service_rpc import invoke
from contracts import collector_pb2, commander_pb2, common_pb2, common_pb2_grpc
from tests.cli.conftest import _write_certs

pytestmark = pytest.mark.contract


def _path(server: Any, service: str, method: str) -> str:
    path: str = server.method_path(service, method)
    return path


def test_mtls_service_info_happy_path(cli_env: Any) -> None:
    cli_env.server.respond_value(
        _path(cli_env.server, "CommonService", "GetServiceInfo"),
        common_pb2.GetServiceInfoResponse(
            service_id="collector", software_version="1.0", contract_version="v1"
        ),
    )
    result = cli_env.invoke("service", "info", "collector")
    assert result.exit_code == 0, result.stdout
    envelope = result.json()
    assert envelope["status"] == "SUCCESS"
    assert envelope["data"]["service_id"] == "collector"


async def test_channel_reused_per_resolved_service(
    cli_env: Any, tmp_path: Path, certs: dict[str, Path]
) -> None:
    runtime = Runtime(cli_env.config_path, "json")
    try:
        assert runtime.channel("collector") is runtime.channel("collector")
    finally:
        await runtime.close()

    # rpc_via 别名解析到底层独占端点；代理身份与进程身份不混用 channel。
    alias_yaml = tmp_path / "alias.yaml"
    alias_yaml.write_text(
        cli_env.config_path.read_text(encoding="utf-8").replace(
            "tls_profiles:\n",
            "  sink_proxy:\n    rpc_via: sink_realtime\ntls_profiles:\n",
        ),
        encoding="utf-8",
    )
    aliased = Runtime(alias_yaml, "json")
    try:
        assert aliased.channel("sink_proxy") is aliased.channel("sink_realtime")
        assert aliased.target("sink_proxy").grpc_endpoint is None
    finally:
        await aliased.close()


def test_rpc_whitelist_rejects_undeclared_method(cli_env: Any) -> None:
    async def scenario() -> None:
        runtime = Runtime(cli_env.config_path, "json")
        try:
            with pytest.raises(ValueError, match="unsupported collector RPC"):
                await invoke(runtime, "collector", "DeleteEverything", {})
        finally:
            await runtime.close()

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,expected_exit,expected_status",
    [
        (grpc.StatusCode.UNAVAILABLE, 3, "UNKNOWN"),
        (grpc.StatusCode.DEADLINE_EXCEEDED, 3, "UNKNOWN"),
        (grpc.StatusCode.UNIMPLEMENTED, 3, "UNKNOWN"),
        (grpc.StatusCode.FAILED_PRECONDITION, 1, None),
        # 设计退出码 2：权限拒绝与参数/配置错误同类。
        (grpc.StatusCode.PERMISSION_DENIED, 2, None),
    ],
)
def test_grpc_error_to_exit_code(
    cli_env: Any,
    code: grpc.StatusCode,
    expected_exit: int,
    expected_status: str | None,
) -> None:
    cli_env.server.fail(
        _path(cli_env.server, "CollectorService", "ListDevices"), code, "injected failure"
    )
    result = cli_env.invoke("collector", "devices")
    assert result.exit_code == expected_exit, result.stdout
    if expected_status is not None:
        assert result.json()["status"] == expected_status


def test_collector_devices_real_protobuf_roundtrip(cli_env: Any) -> None:
    cli_env.server.respond_value(
        _path(cli_env.server, "CollectorService", "ListDevices"),
        collector_pb2.ListDevicesResponse(
            devices=[collector_pb2.Device(device_id="d1", protocol="modbus", revision="r1")]
        ),
    )
    result = cli_env.invoke("collector", "devices")
    assert result.exit_code == 0
    assert result.json()["data"]["devices"][0]["device_id"] == "d1"


def test_commander_write_unknown_outcome_exit3_no_resubmit(cli_env: Any) -> None:
    server = cli_env.server
    server.respond_value(
        _path(server, "CommanderService", "GetDevice"),
        commander_pb2.GetDeviceResponse(
            device=commander_pb2.Device(device_id="d1", revision="rev-1")
        ),
    )
    server.fail(_path(server, "CommanderService", "SubmitCommand"), grpc.StatusCode.UNAVAILABLE)
    result = cli_env.invoke("commander", "write", "d1", "speed", "42.5", "--yes")
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    assert "do not resubmit blindly" in envelope["message"]
    submissions = [call for call in server.calls if call.path.endswith("/SubmitCommand")]
    assert len(submissions) == 1, "UNKNOWN 写入禁止自动重发"
    request = submissions[0].request
    assert request.device_id == "d1"
    assert request.point_id == "speed"
    assert request.value.double_value == pytest.approx(42.5)
    assert request.expected_revision == "rev-1"


def test_commander_write_requires_yes_in_json(cli_env: Any) -> None:
    result = cli_env.invoke("commander", "write", "d1", "p", "1")
    assert result.exit_code == 2
    assert "--yes" in result.stdout


def test_collector_watch_non_overlapping_bounded_polling(cli_env: Any) -> None:
    server = cli_env.server
    server.respond_value(
        _path(server, "CollectorService", "ListPoints"),
        collector_pb2.ListPointsResponse(
            points=[
                common_pb2.PointMetadata(point_id="p1"),
                common_pb2.PointMetadata(point_id="p2"),
            ]
        ),
    )
    inflight = 0
    max_inflight = 0

    async def read(request: Any, context: Any) -> Any:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.1)
        inflight -= 1
        return collector_pb2.ReadPointsResponse(
            device_id=request.device_id,
            samples=[
                common_pb2.Sample(point_id=point, quality="GOOD") for point in request.point_ids
            ],
        )

    server.respond(_path(server, "CollectorService", "ReadPoints"), read)
    started = time.monotonic()
    result = cli_env.invoke("collector", "watch", "d1", "--interval", "0.2", "--count", "3")
    elapsed = time.monotonic() - started
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["iterations"] == 3
    assert max_inflight == 1, "轮询必须非重入"
    # 3 次轮询、间隔 0.2s；即使每次 RPC 耗时 0.1s 也不得重入或跑成固定 sleep 叠加。
    assert elapsed >= 0.4
    reads = [call for call in server.calls if call.path.endswith("/ReadPoints")]
    assert len(reads) == 3


def test_collector_watch_survives_transient_unavailable(cli_env: Any) -> None:
    server = cli_env.server
    server.respond_value(
        _path(server, "CollectorService", "ListPoints"),
        collector_pb2.ListPointsResponse(points=[common_pb2.PointMetadata(point_id="p1")]),
    )
    failures = {"n": 0}

    async def read(request: Any, context: Any) -> Any:
        if failures["n"] < 2:
            failures["n"] += 1
            await context.abort(grpc.StatusCode.UNAVAILABLE, "transient")
        return collector_pb2.ReadPointsResponse(
            device_id="d1", samples=[common_pb2.Sample(point_id="p1", quality="GOOD")]
        )

    server.respond(_path(server, "CollectorService", "ReadPoints"), read)
    result = cli_env.invoke("collector", "watch", "d1", "--interval", "0.2", "--count", "2")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["iterations"] == 2


def test_collector_watch_fails_after_reconnect_budget(cli_env: Any) -> None:
    server = cli_env.server
    server.respond_value(
        _path(server, "CollectorService", "ListPoints"),
        collector_pb2.ListPointsResponse(points=[common_pb2.PointMetadata(point_id="p1")]),
    )
    server.fail(_path(server, "CollectorService", "ReadPoints"), grpc.StatusCode.UNAVAILABLE)
    result = cli_env.invoke("collector", "watch", "d1", "--interval", "0.2", "--count", "1")
    assert result.exit_code == 3


def test_snapshot_push_validate_then_apply_partial_exit4(cli_env: Any, tmp_path: Path) -> None:
    server = cli_env.server
    server.respond_value(
        _path(server, "CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(snapshot_id="s0", revision="r0"),
    )
    server.respond_value(
        _path(server, "CommonService", "PushConfigSnapshot"),
        common_pb2.PushConfigSnapshotResponse(candidate_id="c1", valid=True, base_revision="r0"),
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "collector.yaml").write_text("devices: []\n", encoding="utf-8")
    pushed = cli_env.invoke("config", "push", "collector", str(candidate))
    assert pushed.exit_code == 0, pushed.stdout
    assert pushed.json()["data"]["valid"] is True
    push_calls = [call for call in server.calls if call.path.endswith("/PushConfigSnapshot")]
    assert len(push_calls) == 1
    assert push_calls[0].request.expected_revision == "r0"

    server.respond_value(
        _path(server, "CommonService", "ApplyConfigSnapshot"),
        common_pb2.ApplyConfigSnapshotResponse(
            file_commit="COMMITTED",
            apply_results=[
                common_pb2.ApplicationResult(object_id="task-1", status="SUCCESS"),
                common_pb2.ApplicationResult(object_id="task-2", status="FAILED", error_code="E"),
            ],
            effective_revision="r1",
            restart_required=True,
        ),
    )
    applied = cli_env.invoke("config", "apply", "collector", "c1", "--yes")
    assert applied.exit_code == 4, applied.stdout
    assert applied.json()["status"] == "PARTIAL"


def test_snapshot_apply_full_success_exit0(cli_env: Any) -> None:
    server = cli_env.server
    server.respond_value(
        _path(server, "CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="r1"),
    )
    server.respond_value(
        _path(server, "CommonService", "ApplyConfigSnapshot"),
        common_pb2.ApplyConfigSnapshotResponse(
            file_commit="COMMITTED",
            apply_results=[common_pb2.ApplicationResult(object_id="t", status="APPLIED")],
            effective_revision="r2",
        ),
    )
    result = cli_env.invoke(
        "config",
        "apply",
        "collector",
        "c2",
        "--expected-revision",
        "r1",
        "--yes",
    )
    assert result.exit_code == 0, result.stdout


async def test_mtls_rejects_untrusted_client(
    tmp_path: Path, certs: dict[str, Path], cli_env: Any
) -> None:
    rogue = _write_certs(tmp_path / "rogue")
    credentials = grpc.ssl_channel_credentials(
        root_certificates=rogue["ca"].read_bytes(),
        private_key=rogue["client_key"].read_bytes(),
        certificate_chain=rogue["client_crt"].read_bytes(),
    )
    channel = grpc.aio.secure_channel(f"localhost:{cli_env.server.port}", credentials)
    stub = common_pb2_grpc.CommonServiceStub(channel)
    with pytest.raises(grpc.aio.AioRpcError):
        await stub.GetServiceInfo(common_pb2.Empty(), timeout=3)
    await channel.close()
