"""寄存器编解码与完整写入契约测试。"""

import pytest

from industrial_gateway.infrastructure.protocol.modbus.codec import (
    RegisterMap,
    RegisterMapping,
    decode_registers,
    encode_registers,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("data_type", "value"),
    [("int16", -123), ("uint32", 123456), ("int64", -42), ("float32", 12.5), ("float64", 12.5)],
)
def test_roundtrip(data_type: str, value: int | float) -> None:
    mapping = RegisterMapping("t1", "p", "holding", 100, data_type)
    assert decode_registers(mapping, encode_registers(mapping, value)) == value


def test_word_order() -> None:
    big = RegisterMapping("t1", "p", "holding", 100, "uint32")
    little = RegisterMapping("t1", "p", "holding", 100, "uint32", "little_endian")
    assert encode_registers(big, 0x12345678) == (0x1234, 0x5678)
    assert encode_registers(little, 0x12345678) == (0x5678, 0x1234)


def test_reject_partial_command_and_overlaps() -> None:
    mapping = RegisterMapping("t1", "setpoint", "holding", 100, "float32")
    register_map = RegisterMap([mapping])
    assert register_map.resolve_write("holding", 100, 2) == mapping
    with pytest.raises(ValueError, match="complete"):
        register_map.resolve_write("holding", 100, 1)
    with pytest.raises(ValueError, match="complete"):
        register_map.resolve_write("holding", 101, 1)
    with pytest.raises(ValueError, match="overlapping"):
        RegisterMap([mapping, RegisterMapping("t1", "other", "holding", 101, "uint16")])


def test_invalid_types_and_values() -> None:
    mapping = RegisterMapping("t1", "power", "holding", 0, "float32")
    with pytest.raises(ValueError, match="non-finite"):
        encode_registers(mapping, float("nan"))
    with pytest.raises(ValueError, match="incomplete"):
        decode_registers(mapping, [42])
    with pytest.raises(ValueError, match="address space"):
        RegisterMapping("t1", "power", "holding", 65535, "float32")
