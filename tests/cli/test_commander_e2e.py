"""UC-14 经 Commander 现场写入与结果查询 System Test。

重点：受理（accepted）与最终结果分离、UNKNOWN 不自动重发、非法参数、确认门。
"""

from __future__ import annotations

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
