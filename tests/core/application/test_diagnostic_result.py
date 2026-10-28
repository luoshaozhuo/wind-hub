"""诊断结果领域契约 application/diagnostic.py 的独立验证。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from core.application.diagnostic import (
    DiagnosticFinding,
    DiagnosticResult,
    DiagnosticStatus,
)


def test_status_values_are_stable_strings() -> None:
    assert {s.value for s in DiagnosticStatus} == {
        "PASS",
        "FAIL",
        "WARNING",
        "UNKNOWN",
        "SKIPPED",
    }
    assert DiagnosticStatus.PASS == "PASS"  # StrEnum 语义


def test_to_dict_shape_and_json_compatible_with_special_characters() -> None:
    result = DiagnosticResult(
        category="protocol",
        target="设备-001 🔥",
        status=DiagnosticStatus.FAIL,
        findings=(
            DiagnosticFinding(
                code="PROTOCOL_DIAGNOSTIC_FAILED",
                status=DiagnosticStatus.FAIL,
                message='引号 " 与换行\n',
                location="line 1",
            ),
        ),
        measurements={"rtt_ms": 1.5},
        evidence={"raw": [1, 2, 3]},
    )

    payload = result.to_dict()
    assert payload["category"] == "protocol"
    assert payload["status"] == "FAIL"
    assert payload["findings"][0]["location"] == "line 1"
    # JSON 可序列化是跨 CLI/API 输出的硬契约。
    json.dumps(payload, ensure_ascii=False)


def test_defaults_are_independent_instances() -> None:
    first = DiagnosticResult(category="c", target="t", status=DiagnosticStatus.PASS)
    second = DiagnosticResult(category="c", target="t", status=DiagnosticStatus.PASS)
    first.measurements["x"] = 1
    first.evidence["y"] = 2

    assert second.measurements == {}
    assert second.evidence == {}
    assert first.findings == ()


def test_result_and_finding_are_frozen() -> None:
    finding = DiagnosticFinding(code="C", status=DiagnosticStatus.PASS, message="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.code = "other"  # type: ignore[misc]
