"""commander recover-device：二进制证据传输、非零失败与原文防泄漏。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import pytest

from contracts import commander_pb2 as pb

pytestmark = [pytest.mark.system, pytest.mark.contract]

_EVIDENCE = b"sensitive-attestation-\x00\xff\x80-binary"
_ARGS = (
    "commander",
    "recover-device",
    "--device-id",
    "WT01",
    "--expected-revision",
    "rev-1",
    "--request-id",
    "req-recover-1",
)


def _path(server: Any) -> str:
    return server.method_path("CommanderService", "RecoverDeviceControl")


def test_recover_device_help_exposes_all_required_options(cli_env: Any) -> None:
    result = cli_env.invoke("commander", "recover-device", "--help", format="")
    assert result.exit_code == 0, result.stdout
    for option in ("--device-id", "--expected-revision", "--request-id", "--attestation-file"):
        assert option in result.stdout


def test_binary_attestation_roundtrip_and_blocked_response_is_failure(
    cli_env: Any, tmp_path: Path
) -> None:
    evidence = tmp_path / "attestation.bin"
    evidence.write_bytes(_EVIDENCE)
    cli_env.server.respond_value(
        _path(cli_env.server),
        pb.RecoverDeviceControlResponse(
            device_id="WT01",
            state="BLOCKED",
            decision_id="decision-1",
            evidence_digest="sha256:abc123",
        ),
    )
    result = cli_env.invoke(*_ARGS, "--attestation-file", str(evidence))
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"
    assert result.json()["data"]["state"] == "BLOCKED"
    assert _EVIDENCE.decode("utf-8", errors="ignore") not in result.stdout
    calls = [call for call in cli_env.server.calls if call.path == _path(cli_env.server)]
    assert len(calls) == 1
    request = calls[0].request
    assert request.isolation_attestation == _EVIDENCE
    assert request.device_id == "WT01"
    assert request.expected_revision == "rev-1"
    assert request.request_id == "req-recover-1"


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        (grpc.StatusCode.FAILED_PRECONDITION, 1),
        (grpc.StatusCode.PERMISSION_DENIED, 2),
        (grpc.StatusCode.UNAVAILABLE, 3),
        (grpc.StatusCode.INVALID_ARGUMENT, 2),
    ],
)
def test_recover_device_rpc_failures_are_nonzero_without_evidence_leak(
    cli_env: Any, tmp_path: Path, code: grpc.StatusCode, exit_code: int
) -> None:
    evidence = tmp_path / "attestation.bin"
    evidence.write_bytes(_EVIDENCE)
    cli_env.server.fail(_path(cli_env.server), code, "recovery rejected")
    result = cli_env.invoke(*_ARGS, "--attestation-file", str(evidence))
    assert result.exit_code == exit_code, result.stdout
    assert _EVIDENCE.decode("utf-8", errors="ignore") not in result.stdout


@pytest.mark.parametrize("content", [b"", b"x" * 65537], ids=("empty", "oversized"))
def test_empty_or_oversized_attestation_is_local_failure_without_rpc(
    cli_env: Any, tmp_path: Path, content: bytes
) -> None:
    evidence = tmp_path / "attestation.bin"
    evidence.write_bytes(content)
    result = cli_env.invoke(*_ARGS, "--attestation-file", str(evidence))
    assert result.exit_code != 0, result.stdout
    assert not cli_env.server.calls


def test_missing_attestation_file_is_nonzero_without_rpc(cli_env: Any, tmp_path: Path) -> None:
    result = cli_env.invoke(*_ARGS, "--attestation-file", str(tmp_path / "does-not-exist.bin"))
    assert result.exit_code != 0, result.stdout
    assert not cli_env.server.calls


def test_maximum_size_attestation_is_not_rejected_by_local_bound(
    cli_env: Any, tmp_path: Path
) -> None:
    evidence = tmp_path / "attestation.bin"
    evidence.write_bytes(b"x" * 65536)
    cli_env.server.fail(_path(cli_env.server), grpc.StatusCode.FAILED_PRECONDITION)
    result = cli_env.invoke(*_ARGS, "--attestation-file", str(evidence))
    assert result.exit_code == 1, result.stdout
    assert len(cli_env.server.calls) == 1
    assert cli_env.server.calls[0].request.isolation_attestation == b"x" * 65536
