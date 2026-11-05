"""§14.3 test_grpc.py：正式 Proto 下 14 RPC 真实注册/调用、真实 mTLS 与授权拒绝。

所有用例经过 TCP 回环上的真实 TLS 1.3 握手（require_client_auth），不使用
任何 gRPC 替身；服务端由 build_commander 组合根构造（真实配置树/SQLite/
ConfigSnapshots）。NATS 不可达仅使审计依赖降级。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import commander_pb2, common_pb2
from tests.commander.integration.conftest import IDENTITIES

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.real_service,
]

ALL_RPC_NAMES = (
    "CommonService.GetServiceInfo",
    "CommonService.GetServiceStatus",
    "CommonService.GetCapabilities",
    "CommonService.GetConfigSnapshot",
    "CommonService.PushConfigSnapshot",
    "CommonService.ApplyConfigSnapshot",
    "CommonService.GetMetrics",
    "CommonService.GetEvents",
    "CommonService.Diagnose",
    "CommanderService.ListDevices",
    "CommanderService.GetDevice",
    "CommanderService.ListPoints",
    "CommanderService.SubmitCommand",
    "CommanderService.GetCommandResult",
)


async def _aborts(call, code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await call
    assert exc_info.value.code() is code, exc_info.value
    return exc_info.value


# ---------------------------------------------------------------------------
# 认证 / 授权（真实 mTLS）
# ---------------------------------------------------------------------------


@pytest.mark.normal
async def test_no_client_cert_is_rejected_at_tls(pki, grpc_stack) -> None:
    """服务器强制客户端证书：无证书调用在握手阶段失败，不进入任何业务逻辑。"""
    common, _commander = grpc_stack.stubs(pki, no_client_cert=True)
    err = await _aborts(
        common.GetServiceInfo(common_pb2.Empty(), timeout=5), grpc.StatusCode.UNAVAILABLE
    )
    # 握手失败（SSLV3_ALERT_BAD_CERTIFICATE 等），明确不是业务成功。
    assert "auth" in err.details().lower() or "handshake" in err.details().lower() or err.details()


@pytest.mark.exception
async def test_untrusted_ca_cert_is_rejected(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="rogue", ca=pki.cert("rogue-ca"))
    await _aborts(common.GetServiceInfo(common_pb2.Empty(), timeout=5), grpc.StatusCode.UNAVAILABLE)


@pytest.mark.exception
async def test_unknown_identity_is_permission_denied(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="stranger")
    await _aborts(
        common.GetServiceInfo(common_pb2.Empty(), timeout=5), grpc.StatusCode.PERMISSION_DENIED
    )


@pytest.mark.exception
async def test_multiple_matched_identities_is_permission_denied(pki, grpc_stack) -> None:
    """一张证书同时匹配两个已配置 caller：拒绝，不任选其一放行。"""
    common, _commander = grpc_stack.stubs(pki, client="dual")
    await _aborts(
        common.GetServiceInfo(common_pb2.Empty(), timeout=5), grpc.StatusCode.PERMISSION_DENIED
    )


@pytest.mark.normal
async def test_query_scope_allows_info_and_status(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="viewer")
    info = await common.GetServiceInfo(common_pb2.Empty(), timeout=5)
    assert info.service_id == "commander"
    assert info.process_role == "control"
    assert info.contract_version == "v1"
    assert info.HasField("started_at")
    status = await common.GetServiceStatus(common_pb2.Empty(), timeout=5)
    assert {dep.name for dep in status.dependencies} == {
        "persistence",
        "journal",
        "audit",
        "dispatcher",
    }


@pytest.mark.exception
async def test_reload_and_control_denied_for_query_only_caller(pki, grpc_stack) -> None:
    common, commander = grpc_stack.stubs(pki, client="viewer")
    await _aborts(
        common.PushConfigSnapshot(common_pb2.PushConfigSnapshotRequest(), timeout=5),
        grpc.StatusCode.PERMISSION_DENIED,
    )
    req = commander_pb2.SubmitCommandRequest(
        device_id="TZ01",
        point_id="power_demand",
        expected_revision="x",
        value=common_pb2.ScalarValue(double_value=1.0),
    )
    await _aborts(commander.SubmitCommand(req, timeout=5), grpc.StatusCode.PERMISSION_DENIED)


@pytest.mark.exception
async def test_control_caller_cannot_push_snapshot(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="ops")
    await _aborts(
        common.PushConfigSnapshot(common_pb2.PushConfigSnapshotRequest(), timeout=5),
        grpc.StatusCode.PERMISSION_DENIED,
    )


@pytest.mark.exception
async def test_reload_caller_cannot_submit_command(pki, grpc_stack) -> None:
    _common, commander = grpc_stack.stubs(pki, client="deployer")
    req = commander_pb2.SubmitCommandRequest(
        device_id="TZ01",
        point_id="power_demand",
        expected_revision="x",
        value=common_pb2.ScalarValue(double_value=1.0),
    )
    await _aborts(commander.SubmitCommand(req, timeout=5), grpc.StatusCode.PERMISSION_DENIED)


# ---------------------------------------------------------------------------
# 14 RPC 全注册、可调用（ops/deployer 组合覆盖全部权限域）
# ---------------------------------------------------------------------------


@pytest.mark.normal
async def test_all_fourteen_rpcs_registered_and_callable(pki, grpc_stack) -> None:
    common_ops, commander_ops = grpc_stack.stubs(pki, client="ops")
    common_dep, _commander_dep = grpc_stack.stubs(pki, client="deployer")

    caps = await common_ops.GetCapabilities(common_pb2.Empty(), timeout=5)
    assert tuple(caps.rpc_names) == ALL_RPC_NAMES
    assert len(caps.rpc_names) == 14
    assert "modbus" in tuple(caps.protocols)
    assert "single_point_control" in tuple(caps.features)

    await common_ops.GetServiceInfo(common_pb2.Empty(), timeout=5)
    await common_ops.GetServiceStatus(common_pb2.Empty(), timeout=5)
    disk = await common_ops.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_DISK),
        timeout=5,
    )
    assert len(disk.revision) == 64 and disk.files
    runtime = await common_ops.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_RUNTIME),
        timeout=5,
    )
    assert runtime.revision and {r.object_id for r in runtime.apply_results}

    devices = await commander_ops.ListDevices(commander_pb2.ListDevicesRequest(), timeout=5)
    assert {d.device_id for d in devices.devices} >= {f"TZ0{i}" for i in range(1, 6)}
    assert all(len(d.revision) == 64 for d in devices.devices)
    device = await commander_ops.GetDevice(common_pb2.ObjectId(id="TZ01"), timeout=5)
    assert device.device.device_id == "TZ01"
    assert device.device.protocol == "modbus"
    points = await commander_ops.ListPoints(
        commander_pb2.ListPointsRequest(device_id="TZ01"), timeout=5
    )
    assert points.points

    # 候选生命周期（全量磁盘文件原样推送，必然校验通过）。
    pushed = await common_dep.PushConfigSnapshot(
        common_pb2.PushConfigSnapshotRequest(files=disk.files, expected_revision=disk.revision),
        timeout=30,
    )
    assert pushed.valid is True and pushed.candidate_id
    cand = await common_dep.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(
            scope=common_pb2.SNAPSHOT_SCOPE_CANDIDATE, candidate_id=pushed.candidate_id
        ),
        timeout=5,
    )
    assert cand.revision == disk.revision
    applied = await common_dep.ApplyConfigSnapshot(
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id, expected_revision=disk.revision
        ),
        timeout=30,
    )
    assert applied.file_commit == disk.revision
    assert applied.restart_required is False

    metrics = await common_ops.GetMetrics(
        common_pb2.GetMetricsRequest(scope="commander", window_ms=1000), timeout=5
    )
    assert metrics.metrics and all(m.HasField("sampled_at") for m in metrics.metrics)
    # 第 8 个 RPC GetEvents 同样必须真实可调用（14 RPC 覆盖的一部分）。
    await common_ops.GetEvents(common_pb2.GetEventsRequest(), timeout=5)
    diagnose = await common_ops.Diagnose(
        common_pb2.DiagnoseRequest(stages=["service", "configuration"], timeout_ms=2000),
        timeout=5,
    )
    assert {s.stage for s in diagnose.stages} == {"service", "configuration"}
    assert diagnose.limitations

    # SubmitCommand + GetCommandResult（接受为内存语义；TZ01:1502 无对端，
    # 接受成功后异步连接失败不影响接受返回）。按点表真实类型选择可 coerce 的值。
    writable = next(p for p in points.points if p.writable)
    value = (
        common_pb2.ScalarValue(double_value=1.0)
        if "float" in writable.data_type
        else common_pb2.ScalarValue(sint64_value=1)
    )
    revision = next(d.revision for d in devices.devices if d.device_id == "TZ01")
    submitted = await commander_ops.SubmitCommand(
        commander_pb2.SubmitCommandRequest(
            device_id="TZ01",
            point_id=writable.point_id,
            value=value,
            expected_revision=revision,
        ),
        timeout=5,
    )
    assert submitted.accepted is True
    assert submitted.state == "ACCEPTED"
    assert submitted.command_id
    result = await commander_ops.GetCommandResult(
        commander_pb2.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
    )
    assert result.command_id == submitted.command_id
    assert result.device_id == "TZ01"
    assert result.point_id == writable.point_id
    assert result.HasField("updated_at")
    # UNKNOWN 与 io_settled 是两个独立维度，字段必然存在（bool）。
    assert isinstance(result.io_settled, bool)


# ---------------------------------------------------------------------------
# ScalarValue oneof / Timestamp / 分页 / 错误码 / 边界
# ---------------------------------------------------------------------------


async def _revision(grpc_stack, pki) -> tuple[str, str, str]:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    devices = await commander.ListDevices(commander_pb2.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "TZ01")
    points = await commander.ListPoints(
        commander_pb2.ListPointsRequest(device_id="TZ01"), timeout=5
    )
    point = next(p for p in points.points if p.writable).point_id
    return "TZ01", point, revision


@pytest.mark.normal
@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: common_pb2.ScalarValue(sint64_value=-7), id="sint64"),
        pytest.param(lambda: common_pb2.ScalarValue(uint64_value=7), id="uint64"),
        pytest.param(lambda: common_pb2.ScalarValue(double_value=3.0), id="double-integral"),
    ],
)
async def test_scalar_value_numeric_kinds_accept_on_int_point(pki, grpc_stack, factory) -> None:
    """TZ 真实点表为原始整数点：三种数值 oneof 分支均被适配器解码并接受。"""
    _common, commander = grpc_stack.stubs(pki, client="ops")
    device, point, revision = await _revision(grpc_stack, pki)
    submitted = await commander.SubmitCommand(
        commander_pb2.SubmitCommandRequest(
            device_id=device,
            point_id=point,
            value=factory(),
            expected_revision=revision,
        ),
        timeout=5,
    )
    assert submitted.accepted is True


@pytest.mark.exception
@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: common_pb2.ScalarValue(bool_value=True), id="bool"),
        pytest.param(lambda: common_pb2.ScalarValue(string_value="remote"), id="string"),
    ],
)
async def test_scalar_value_bool_string_decode_then_typed_by_point(
    pki, grpc_stack, factory
) -> None:
    """bool/string 分支被适配器正常解码，业务点类型不匹配由授权层拒绝。

    真实配置树中没有 bool/string 类型可写点；关键断言是拒绝发生在授权阶段
    （PERMISSION_DENIED「not authorized」），而不是 oneof 适配层
    INVALID_ARGUMENT——证明 six-kind 解码分支被真实执行。
    """
    _common, commander = grpc_stack.stubs(pki, client="ops")
    device, point, revision = await _revision(grpc_stack, pki)
    err = await _aborts(
        commander.SubmitCommand(
            commander_pb2.SubmitCommandRequest(
                device_id=device,
                point_id=point,
                value=factory(),
                expected_revision=revision,
            ),
            timeout=5,
        ),
        grpc.StatusCode.PERMISSION_DENIED,
    )
    assert "not authorized" in err.details()


@pytest.mark.normal
@pytest.mark.modbus
async def test_scalar_value_double_accepts_on_real_float_point(pki, grpc_stack_modbus) -> None:
    """double 分支经真实 gRPC→真实 Modbus 对端写 float32 点并可查结果。"""
    _common, commander = grpc_stack_modbus.stubs(pki, client="ops")
    devices = await commander.ListDevices(commander_pb2.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")
    submitted = await commander.SubmitCommand(
        commander_pb2.SubmitCommandRequest(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=42.5),
            expected_revision=revision,
        ),
        timeout=5,
    )
    assert submitted.accepted is True
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    while datetime.now(UTC) < deadline:
        result = await commander.GetCommandResult(
            commander_pb2.GetCommandResultRequest(command_id=submitted.command_id),
            timeout=5,
        )
        if result.state in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            break
        await asyncio.sleep(0.05)
    assert result.state == "SUCCEEDED"
    assert result.outcome == "CONFIRMED_SUCCESS"
    assert result.io_settled is True


@pytest.mark.exception
async def test_scalar_value_bytes_kind_rejected(pki, grpc_stack) -> None:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    device, point, revision = await _revision(grpc_stack, pki)
    await _aborts(
        commander.SubmitCommand(
            commander_pb2.SubmitCommandRequest(
                device_id=device,
                point_id=point,
                value=common_pb2.ScalarValue(bytes_value=b"\x01"),
                expected_revision=revision,
            ),
            timeout=5,
        ),
        grpc.StatusCode.INVALID_ARGUMENT,
    )


@pytest.mark.exception
async def test_scalar_value_unset_kind_rejected(pki, grpc_stack) -> None:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    device, _point, revision = await _revision(grpc_stack, pki)
    await _aborts(
        commander.SubmitCommand(
            commander_pb2.SubmitCommandRequest(
                device_id=device,
                point_id="anything",
                value=common_pb2.ScalarValue(),
                expected_revision=revision,
            ),
            timeout=5,
        ),
        grpc.StatusCode.INVALID_ARGUMENT,
    )


@pytest.mark.exception
async def test_submit_boundary_validation(pki, grpc_stack) -> None:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    device, point, revision = await _revision(grpc_stack, pki)
    good_value = common_pb2.ScalarValue(double_value=1.0)

    async def expect_invalid(**kwargs: object) -> None:
        await _aborts(
            commander.SubmitCommand(commander_pb2.SubmitCommandRequest(**kwargs), timeout=5),
            grpc.StatusCode.INVALID_ARGUMENT,
        )

    await expect_invalid(device_id="", point_id=point, value=good_value, expected_revision=revision)
    await expect_invalid(
        device_id=device, point_id="", value=good_value, expected_revision=revision
    )
    await expect_invalid(device_id=device, point_id=point, value=good_value, expected_revision="")
    past = Timestamp()
    past.FromDatetime(datetime.now(UTC) - timedelta(seconds=10))
    await expect_invalid(
        device_id=device,
        point_id=point,
        value=good_value,
        expected_revision=revision,
        expires_at=past,
    )


@pytest.mark.exception
async def test_missing_entities_map_to_not_found(pki, grpc_stack) -> None:
    common, commander = grpc_stack.stubs(pki, client="ops")
    await _aborts(
        commander.GetDevice(common_pb2.ObjectId(id="NOPE"), timeout=5),
        grpc.StatusCode.NOT_FOUND,
    )
    await _aborts(
        commander.ListPoints(commander_pb2.ListPointsRequest(device_id="NOPE"), timeout=5),
        grpc.StatusCode.NOT_FOUND,
    )
    await _aborts(
        commander.GetCommandResult(
            commander_pb2.GetCommandResultRequest(command_id="missing"), timeout=5
        ),
        grpc.StatusCode.NOT_FOUND,
    )
    await _aborts(
        common.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(
                scope=common_pb2.SNAPSHOT_SCOPE_CANDIDATE, candidate_id="missing"
            ),
            timeout=5,
        ),
        grpc.StatusCode.NOT_FOUND,
    )


@pytest.mark.normal
async def test_device_pagination_token_walk(pki, grpc_stack) -> None:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    page1 = await commander.ListDevices(commander_pb2.ListDevicesRequest(page_size=2), timeout=5)
    assert len(page1.devices) == 2 and page1.next_page_token == "2"
    page2 = await commander.ListDevices(
        commander_pb2.ListDevicesRequest(page_size=2, page_token=page1.next_page_token),
        timeout=5,
    )
    ids1 = {d.device_id for d in page1.devices}
    ids2 = {d.device_id for d in page2.devices}
    assert not ids1 & ids2
    # token 走完整本列表。
    seen = ids1 | ids2
    token = page2.next_page_token
    while token:
        page = await commander.ListDevices(
            commander_pb2.ListDevicesRequest(page_size=2, page_token=token), timeout=5
        )
        seen |= {d.device_id for d in page.devices}
        token = page.next_page_token
    full = await commander.ListDevices(commander_pb2.ListDevicesRequest(), timeout=5)
    assert seen == {d.device_id for d in full.devices}


@pytest.mark.exception
async def test_pagination_boundary_validation(pki, grpc_stack) -> None:
    _common, commander = grpc_stack.stubs(pki, client="ops")
    await _aborts(
        commander.ListDevices(commander_pb2.ListDevicesRequest(page_size=1001), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        commander.ListDevices(commander_pb2.ListDevicesRequest(page_token="abc"), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        commander.ListDevices(commander_pb2.ListDevicesRequest(page_token="999999"), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )


@pytest.mark.exception
async def test_metrics_scope_and_window_validation(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="ops")
    await _aborts(
        common.GetMetrics(common_pb2.GetMetricsRequest(scope="cluster", window_ms=1), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.GetMetrics(
            common_pb2.GetMetricsRequest(scope="commander", window_ms=300_001),
            timeout=5,
        ),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.GetMetrics(common_pb2.GetMetricsRequest(scope="device:", window_ms=0), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.GetMetrics(
            common_pb2.GetMetricsRequest(scope="device:NOPE", window_ms=0), timeout=5
        ),
        grpc.StatusCode.NOT_FOUND,
    )
    scoped = await common.GetMetrics(
        common_pb2.GetMetricsRequest(scope="device:TZ01", window_ms=0), timeout=5
    )
    assert {m.name for m in scoped.metrics} == {"device_queue_depth"}


@pytest.mark.exception
async def test_events_and_diagnose_argument_validation(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="ops")
    earlier = Timestamp()
    earlier.FromDatetime(datetime.now(UTC) - timedelta(seconds=60))
    later = Timestamp()
    later.FromDatetime(datetime.now(UTC) - timedelta(seconds=30))
    await _aborts(
        common.GetEvents(common_pb2.GetEventsRequest(from_time=later, to_time=earlier), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.Diagnose(common_pb2.DiagnoseRequest(stages=["bogus"]), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.Diagnose(common_pb2.DiagnoseRequest(stages=["device"]), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )


@pytest.mark.normal
async def test_diagnose_device_tcp_stage_reports_unreachable_port(pki, grpc_stack) -> None:
    """tcp 阶段只验证可达性，不发起协议握手，更不产生任何控制写入。"""
    common, _commander = grpc_stack.stubs(pki, client="ops")
    response = await common.Diagnose(
        common_pb2.DiagnoseRequest(stages=["tcp"], target_id="TZ01", timeout_ms=3000),
        timeout=8,
    )
    stage = next(s for s in response.stages if s.stage == "tcp")
    # TZ01 指向 127.0.0.1:1502，测试环境无真实从站：FAIL 且证据只是端口探测。
    assert stage.status == "FAIL"
    assert any(e.name == "tcp_endpoint" for e in stage.evidence)


def test_callers_config_matches_permission_design() -> None:
    """§9.3.1：身份→caller_id→授权域的夹具配置本身可审计。"""
    assert IDENTITIES["viewer"][2] == ("query",)
    assert set(IDENTITIES["ops"][2]) == {"query", "control"}
    assert set(IDENTITIES["deployer"][2]) == {"query", "reload"}
