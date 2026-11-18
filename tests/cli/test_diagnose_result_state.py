"""所有单服务与跨服务诊断命令的统一退出码契约。"""

from __future__ import annotations

import pytest

from cli.runtime import Runtime


@pytest.mark.parametrize("command", ["diagnose pipeline", "collector Diagnose", "sink Diagnose"])
@pytest.mark.parametrize(
    ("overall", "expected"),
    [
        ("PASS", ("SUCCESS", 0)),
        ("SUCCESS", ("SUCCESS", 0)),
        ("FAIL", ("FAILURE", 1)),
        ("FAILED", ("FAILURE", 1)),
        ("FAILURE", ("FAILURE", 1)),
        ("PARTIAL", ("UNKNOWN", 3)),
        ("UNKNOWN", ("UNKNOWN", 3)),
        ("NOT_RUN", ("UNKNOWN", 3)),
    ],
)
def test_diagnose_status_controls_exit_code(
    command: str, overall: str, expected: tuple[str, int]
) -> None:
    assert Runtime._result_state(command, {"overall_status": overall}) == expected
