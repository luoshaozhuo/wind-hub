"""诊断结果契约：独立于 CLI、gRPC 和具体检测器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DiagnosticStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    code: str
    status: DiagnosticStatus
    message: str
    location: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    category: str
    target: str
    status: DiagnosticStatus
    findings: tuple[DiagnosticFinding, ...] = ()
    measurements: dict[str, int | float | str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """输出跨 CLI/API 通用的 JSON 兼容结果。"""
        return {
            "category": self.category,
            "target": self.target,
            "status": self.status.value,
            "findings": [
                {
                    "code": item.code,
                    "status": item.status.value,
                    "message": item.message,
                    "location": item.location,
                }
                for item in self.findings
            ],
            "measurements": dict(self.measurements),
            "evidence": dict(self.evidence),
        }
