"""Core 诊断结果模型作为独立进程共享的值对象。"""

from dataclasses import FrozenInstanceError

import pytest

from core.domain.diagnostic import (
    DeviceVerifyResult,
    DiagnosticCode,
    DiagnosticSeverity,
    DiagnosticStage,
    ResolvedAddress,
)


def test_diagnostic_codes_and_stage_are_stable() -> None:
    stage = DiagnosticStage(
        name="network",
        ok=False,
        code=DiagnosticCode.PING_FAILED,
        severity=DiagnosticSeverity.WARNING,
    )
    assert stage.code.value == "PING_FAILED"
    assert stage.severity.value == "WARNING"
    assert stage.message == ""
    with pytest.raises(FrozenInstanceError):
        stage.ok = True  # type: ignore[misc]


def test_device_result_preserves_stages() -> None:
    result = DeviceVerifyResult(
        device_id="device1",
        protocol="modbus",
        host="127.0.0.1",
        port=502,
        ok=True,
    )
    assert result.stages == ()
    assert result.ok


def test_resolved_address_is_immutable() -> None:
    address = ResolvedAddress(
        symbol=None,
        index_group=1,
        index_offset=2,
        size=4,
        protocol_type="REAL",
    )
    assert address.index_offset == 2
    with pytest.raises(FrozenInstanceError):
        address.index_offset = 3  # type: ignore[misc]
