"""Core protocol scalar normalization across ADS/Modbus/IEC104."""

import pytest

from core.domain.protocol_types import normalize_protocol_scalar_type
from core.domain.value_objects import DataType


@pytest.mark.parametrize(
    "protocol, source, expected",
    [
        ("ads", "REAL", DataType.FLOAT32),
        ("ads", "LREAL", DataType.FLOAT64),
        ("ads", "SINT", DataType.INT8),
        ("ads", "USINT", DataType.UINT8),
        ("ads", "DINT", DataType.INT32),
        ("ads", "ULINT", DataType.UINT64),
        ("ads", "STRING", DataType.STRING),
        ("modbus", "float32", DataType.FLOAT32),
        ("modbus", "uint16", DataType.UINT16),
        ("iec104", "int32", DataType.INT32),
        ("iec104", "bool", DataType.BOOL),
    ],
)
def test_normalization(protocol: str, source: str, expected: DataType) -> None:
    assert normalize_protocol_scalar_type(protocol, source) is expected


@pytest.mark.parametrize(
    "protocol, source",
    [("ads", "POINTER"), ("modbus", "REAL"), ("iec104", ""), ("other", "int32")],
)
def test_reject_unrecognized_protocol_types(protocol: str, source: str) -> None:
    with pytest.raises(ValueError):
        normalize_protocol_scalar_type(protocol, source)
