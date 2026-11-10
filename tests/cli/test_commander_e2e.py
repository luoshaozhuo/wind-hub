"""UC-14 经 Commander 现场写入与结果查询 System Test。

重点：受理（accepted）与最终结果分离、UNKNOWN 不自动重发、非法参数、确认门。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest

from contracts import commander_pb2 as cmd

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _p(server: Any, method: str) -> str:
    return server.method_path("CommanderService", method)


def test_commander_write_accepted_then_poll_final_result(cli_env: Any) -> None:
    """S01：write 受理返回 command_id；result 查询最终状态，两者不混淆。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "GetDevice"),
        cmd.GetDeviceResponse(device=cmd.Device(device_id="d1", revision="rev-1")),
    )
    server.respond_value(
        _p(server, "SubmitCommand"),
        cmd.SubmitCommandResponse(
            command_id="c-100", accepted=True, state="ACCEPTED", outcome="PENDING"
        ),
    )
    server.respond_value(
        _p(server, "GetCommandResult"),
        cmd.GetCommandResultResponse(
            command_id="c-100", state="COMPLETED", outcome="SUCCESS", io_settled=True
        ),
    )
    submitted = cli_env.subprocess_invoke("commander", "write", "d1", "speed", "42.5", "--yes")
    assert submitted.exit_code == 0, submitted.stdout
    data = submitted.json()["data"]
    assert data["accepted"] is True
    assert data["command_id"] == "c-100"
    assert data["state"] == "ACCEPTED"  # 受理 ≠ 最终成功

    result = cli_env.subprocess_invoke("commander", "result", "c-100")
    assert result.exit_code == 0, result.stdout
    final = result.json()["data"]
    assert final["state"] == "COMPLETED"
    assert final["io_settled"] is True


def test_commander_write_confirm_gate_and_illegal_scalar(cli_env: Any) -> None:
    """S02：无 --yes 不发 RPC；非法标量退出 2。"""
    denied = cli_env.subprocess_invoke("commander", "write", "d1", "p", "1")
    assert denied.exit_code == 2
    assert not cli_env.server.calls

    bad = cli_env.subprocess_invoke("commander", "write", "d1", "p", '{"a":1}', "--yes")
    assert bad.exit_code == 2, bad.stdout
    assert not cli_env.server.calls


def test_commander_write_unknown_no_resubmit(cli_env: Any) -> None:
    """S03：UNAVAILABLE → UNKNOWN/3，只提交一次，message 含禁止盲目重发提示。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "GetDevice"),
        cmd.GetDeviceResponse(device=cmd.Device(device_id="d1", revision="r")),
    )
    server.fail(_p(server, "SubmitCommand"), grpc.StatusCode.UNAVAILABLE, "lost")
    result = cli_env.subprocess_invoke("commander", "write", "d1", "speed", "1", "--yes")
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    assert "do not resubmit blindly" in envelope["message"]
    assert len([c for c in server.calls if c.path.endswith("/SubmitCommand")]) == 1


def test_reused_request_id_requires_explicit_expiry(cli_env: Any) -> None:
    """S04：复用 --request-id 时缺 --expires-at/--expected-revision 本地拒绝，不发任何 RPC。"""
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    missing_expiry = cli_env.subprocess_invoke(
        "commander",
        "write",
        "d1",
        "speed",
        "1",
        "--request-id",
        "req-1",
        "--expected-revision",
        "rev-1",
        "--yes",
    )
    assert missing_expiry.exit_code == 2, missing_expiry.stdout
    assert "expires-at" in missing_expiry.stdout
    assert not cli_env.server.calls

    missing_revision = cli_env.subprocess_invoke(
        "commander",
        "write",
        "d1",
        "speed",
        "1",
        "--request-id",
        "req-1",
        "--expires-at",
        expiry,
        "--yes",
    )
    assert missing_revision.exit_code == 2, missing_revision.stdout
    assert "expected-revision" in missing_revision.stdout
    assert not cli_env.server.calls  # 不得降级为现场 GetDevice 补 revision


def test_commander_write_replayed_request_keeps_stable_revision_and_expiry(
    cli_env: Any,
) -> None:
    """S04：同 request_id 重放携带完全一致的 expires/revision，不触发 GetDevice、不换新 ID。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "SubmitCommand"),
        cmd.SubmitCommandResponse(
            command_id="c-200", accepted=True, state="ACCEPTED", outcome="PENDING"
        ),
    )
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    common_args = (
        "commander",
        "write",
        "d1",
        "speed",
        "42.5",
        "--request-id",
        "req-stable-1",
        "--expires-at",
        expiry,
        "--expected-revision",
        "rev-7",
        "--yes",
    )
    first = cli_env.subprocess_invoke(*common_args)
    second = cli_env.subprocess_invoke(*common_args)
    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout

    get_device_calls = [c for c in server.calls if c.path.endswith("/GetDevice")]
    assert get_device_calls == []  # 显式 revision 时不得查询设备
    submits = [c for c in server.calls if c.path.endswith("/SubmitCommand")]
    assert len(submits) == 2
    expected_epoch = int(datetime.fromisoformat(expiry).timestamp())
    for record in submits:
        assert record.request.request_id == "req-stable-1"
        assert record.request.expected_revision == "rev-7"
        assert record.request.expires_at.seconds == expected_epoch
    # 两次线载荷幂等键与关键围栏字段完全一致，由服务端幂等去重而非 CLI 换新 ID。
    assert submits[0].request.request_id == submits[1].request.request_id
    assert submits[0].request.expires_at == submits[1].request.expires_at
    assert submits[0].request.expected_revision == submits[1].request.expected_revision


def test_commander_write_rejected_precondition_exit1(cli_env: Any) -> None:
    """S03：服务端 FAILED_PRECONDITION（如设备离线）→ 1。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "GetDevice"),
        cmd.GetDeviceResponse(device=cmd.Device(device_id="d1", revision="r")),
    )
    server.fail(_p(server, "SubmitCommand"), grpc.StatusCode.FAILED_PRECONDITION, "device offline")
    result = cli_env.subprocess_invoke("commander", "write", "d1", "p", "1", "--yes")
    assert result.exit_code == 1, result.stdout
