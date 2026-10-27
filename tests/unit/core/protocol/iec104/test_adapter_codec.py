"""IEC104 命令转换与监视品质规则单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ConfigError
from core.application.protocol_contract import Quality
from core.infrastructure.protocol.iec104.codec import (
    build_command,
    decode_quality,
)
from core.infrastructure.protocol.iec104.mapping import IEC104Point


def _mapped(type_id: str) -> IEC104Point:
    return IEC104Point(point_id="p", common_address=1, ioa=100, type_id=type_id)


def test_single_command_requires_bool_and_encodes_on() -> None:
    command = build_command(_mapped("C_SC_NA_1"), True)
    assert isinstance(command, iec101.SingleCommand)
    assert command.value is iec101.SingleValue.ON
    assert command.select is False
    with pytest.raises(TypeError):
        build_command(_mapped("C_SC_NA_1"), 1)


@pytest.mark.parametrize("value", [1, 2])
def test_double_command_values(value: int) -> None:
    command = build_command(_mapped("C_DC_NA_1"), value)
    assert isinstance(command, iec101.DoubleCommand)
    assert command.value is (iec101.DoubleValue.OFF if value == 1 else iec101.DoubleValue.ON)


@pytest.mark.parametrize("value", [0, 3, True])
def test_double_command_rejects_invalid_values(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        build_command(_mapped("C_DC_NA_1"), value)


def test_normalized_command_range() -> None:
    assert isinstance(build_command(_mapped("C_SE_NA_1"), 0.5), iec101.NormalizedCommand)
    with pytest.raises(ValueError):
        build_command(_mapped("C_SE_NA_1"), 1.5)


def test_scaled_command_integer_range() -> None:
    assert isinstance(build_command(_mapped("C_SE_NB_1"), -32768), iec101.ScaledCommand)
    with pytest.raises(ValueError):
        build_command(_mapped("C_SE_NB_1"), 32768)


def test_floating_command_rejects_non_finite_value() -> None:
    assert isinstance(build_command(_mapped("C_SE_NC_1"), 12.5), iec101.FloatingCommand)
    with pytest.raises(ValueError, match="finite"):
        build_command(_mapped("C_SE_NC_1"), float("nan"))


def test_unsupported_command_does_not_fall_back() -> None:
    with pytest.raises(ConfigError, match="unsupported command"):
        build_command(_mapped("C_RC_NA_1"), 1)


def test_monitoring_quality_precedence() -> None:
    assert decode_quality(None) is Quality.UNCERTAIN
    assert decode_quality(SimpleNamespace(invalid=False, blocked=False)) is Quality.GOOD
    assert decode_quality(SimpleNamespace(invalid=False, blocked=True)) is Quality.UNCERTAIN
    assert decode_quality(SimpleNamespace(invalid=True, blocked=True)) is Quality.BAD
