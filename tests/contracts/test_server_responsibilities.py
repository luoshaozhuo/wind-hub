"""Proto3 无法在 wire 层强制的约束：确认“可表达性”，并钉死 Server 校验责任。

设计书 9.1 要求必需参数必须校验、快照路径必须为规范化相对路径、冲突使用
ABORTED 等。Proto 本身不承担这些业务校验，本测试只验证契约具备表达所需
输入/输出的能力，并固化「以下条件必须由各 Server 实现运行时校验」的清单，
防止后续阶段把 wire 可构造误判为校验已实现。
"""

from __future__ import annotations

import pytest

from contracts import collector_pb2, common_pb2

pytestmark = pytest.mark.contract

# （输入消息类型，字段）——wire 层允许空值/非法值，Server 必须拒绝：
# 1. ReadPoints.point_ids 非空（设计书 9.3.2）；
# 2. GetConfigSnapshot scope=CANDIDATE 时 candidate_id 必填（9.2.2）；
# 3. 快照 relative_path 拒绝 ..、绝对路径、符号链接与重复项（9.2.2 末段）；
# 4. page_size 服务端上限（9.1 分页行）。
SERVER_VALIDATED_REQUIREMENTS = (
    "ReadPointsRequest.point_ids 非空",
    "GetConfigSnapshotRequest CANDIDATE 作用域必须携带 candidate_id",
    "SnapshotFile.relative_path 规范化相对路径（拒绝 ../绝对路径/symlink/重复）",
    "分页 page_size 服务端上限",
    "PushConfigSnapshot 校验失败不得产生可应用候选（valid=False 表达）",
    "版本冲突以 gRPC ABORTED 返回（传输层状态码，非消息字段）",
)


def test_wire_accepts_empty_point_ids_server_must_reject() -> None:
    request = collector_pb2.ReadPointsRequest(device_id="d1", point_ids=[], timeout_ms=100)
    decoded = collector_pb2.ReadPointsRequest()
    decoded.ParseFromString(request.SerializeToString())
    assert decoded.point_ids == []  # 非空校验只能由 Server 返回 INVALID_ARGUMENT。


def test_candidate_scope_without_candidate_id_is_expressible_server_must_reject() -> None:
    request = common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_CANDIDATE)
    assert request.scope == common_pb2.SNAPSHOT_SCOPE_CANDIDATE
    assert request.candidate_id == ""  # Server 必须对候选查询强制 candidate_id。


def test_unsafe_relative_path_is_wire_expressible_server_must_reject() -> None:
    for unsafe in ("../etc/passwd", "/abs/path.yaml", "dir/../../x.yaml"):
        message = common_pb2.SnapshotFile(relative_path=unsafe, content=b"", size_bytes=0)
        decoded = common_pb2.SnapshotFile()
        decoded.ParseFromString(message.SerializeToString())
        assert decoded.relative_path == unsafe  # 路径安全由 Server 校验。


def test_push_failure_can_be_reported_without_applicable_candidate() -> None:
    response = common_pb2.PushConfigSnapshotResponse(
        candidate_id="",
        valid=False,
        validation=[common_pb2.ValidationIssue(path="a.yaml", code="PARSE_ERROR", message="bad")],
        base_revision="r1",
    )
    decoded = common_pb2.PushConfigSnapshotResponse()
    decoded.ParseFromString(response.SerializeToString())
    assert decoded.valid is False
    assert decoded.candidate_id == ""
    assert len(decoded.validation) == 1


def test_operation_result_can_carry_business_status_and_error() -> None:
    # gRPC 传输错误与业务执行状态分离：失败仍以正常响应返回逐对象状态。
    result = common_pb2.OperationResult(
        operation_id="op-1",
        accepted=False,
        desired_state="RUNNING",
        actual_state="STOPPED",
        revision="r2",
        error_code="FAILED_PRECONDITION",
        message="watchdog fence active",
    )
    decoded = common_pb2.OperationResult()
    decoded.ParseFromString(result.SerializeToString())
    assert decoded.accepted is False
    assert decoded.error_code == "FAILED_PRECONDITION"


def test_documented_requirement_list_is_explicit() -> None:
    # 钉死清单：契约冻结时这些语义已确认归属 Server，Proto 层不提供 enforcement。
    assert len(SERVER_VALIDATED_REQUIREMENTS) == 6
