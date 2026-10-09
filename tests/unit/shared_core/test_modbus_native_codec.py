"""PyModbus 原生多寄存器转换与既有 Modbus 语义的回归测试。"""

from __future__ import annotations

import pytest

from core.infrastructure.protocol.modbus.driver import _decode_registers, _encode_registers

pytest.importorskip("pymodbus")


@pytest.mark.parametrize(
    ("data_type", "value", "registers"),
    [
        ("int32", -1, [0xFFFF, 0xFFFF]),
        ("uint32", 0x12345678, [0x1234, 0x5678]),
        ("float32", 1.0, [0x3F80, 0x0000]),
        ("int64", -1, [0xFFFF] * 4),
        ("uint64", 0x123456789ABCDEF0, [0x1234, 0x5678, 0x9ABC, 0xDEF0]),
        ("float64", 1.0, [0x3FF0, 0x0000, 0x0000, 0x0000]),
    ],
)
@pytest.mark.parametrize("word_order", ["big_endian", "little_endian"])
def test_multi_register_roundtrip(
    data_type: str, value: int | float, registers: list[int], word_order: str
) -> None:
    expected = registers if word_order == "big_endian" else list(reversed(registers))
    assert _encode_registers(value, data_type, word_order) == expected
    assert _decode_registers(expected, data_type, word_order) == value


@pytest.mark.parametrize(
    ("data_type", "raw", "expected"),
    [
        ("bool", 0x0002, False),
        ("bool", 0x0001, True),
        ("int8", 0x00FF, -1),
        ("uint8", 0x12AB, 0xAB),
        ("int16", 0xFFFF, -1),
        ("uint16", 0xFFFF, 65535),
    ],
)
def test_single_register_low_bits_and_signed_values(
    data_type: str, raw: int, expected: int | bool
) -> None:
    assert _decode_registers([raw], data_type, "big_endian") == expected
