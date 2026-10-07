"""Modbus Sink 内存数据区。

根据 ResolvedSinkPoint 预建 unit_id 下的 coil/discrete/holding/input 稀疏地址空间，
并接收 EncodedModbusValue 更新。该层不依赖 pymodbus，也不负责 TCP Server。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collector.application.sinks import (
    MODBUS_WORD_WIDTH,
    ModbusSinkAddress,
    ResolvedSinkPoint,
)
from collector.infrastructure.sink.modbus.codec import EncodedModbusValue


@dataclass(slots=True)
class ModbusUnitStore:
    """单个 Modbus unit 的四类地址空间。"""

    coil: dict[int, bool] = field(default_factory=dict)
    discrete: dict[int, bool] = field(default_factory=dict)
    holding: dict[int, int] = field(default_factory=dict)
    input: dict[int, int] = field(default_factory=dict)


class ModbusSinkStore:
    """按 unit_id 组织的 Modbus Sink 稀疏内存模型。"""

    def __init__(self, points: list[ResolvedSinkPoint]) -> None:
        self._units: dict[int, ModbusUnitStore] = {}
        for point in points:
            address = point.address
            if not isinstance(address, ModbusSinkAddress):
                continue
            unit = self._units.setdefault(address.unit_id, ModbusUnitStore())
            if address.register_type in {"coil", "discrete"}:
                self._bit_space(unit, address.register_type)[address.address] = False
                continue

            width = MODBUS_WORD_WIDTH[point.datatype]
            registers = self._register_space(unit, address.register_type)
            for offset in range(width):
                registers[address.address + offset] = 0

    @property
    def unit_ids(self) -> tuple[int, ...]:
        """返回已声明 unit_id，按升序排列。"""
        return tuple(sorted(self._units))

    def write(self, value: EncodedModbusValue) -> None:
        """把已编码值写入预声明地址空间。"""
        unit = self._units.get(value.unit_id)
        if unit is None:
            raise KeyError(f"Unknown Modbus unit_id {value.unit_id}")

        if value.register_type in {"coil", "discrete"}:
            bit_space = self._bit_space(unit, value.register_type)
            if value.address not in bit_space:
                raise KeyError(
                    f"Undeclared Modbus address {value.unit_id}/"
                    f"{value.register_type}/{value.address}"
                )
            addresses = [value.address + offset for offset in range(len(value.bits))]
            self._require_declared(
                bit_space,
                value.unit_id,
                value.register_type,
                addresses,
            )
            for address, bit in zip(addresses, value.bits, strict=True):
                bit_space[address] = bit
            return

        register_space = self._register_space(unit, value.register_type)
        addresses = [value.address + offset for offset in range(len(value.registers))]
        self._require_declared(
            register_space,
            value.unit_id,
            value.register_type,
            addresses,
        )
        for address, register in zip(addresses, value.registers, strict=True):
            register_space[address] = register

    def read_bits(
        self,
        unit_id: int,
        register_type: str,
        address: int,
        count: int = 1,
    ) -> list[bool]:
        """读取已声明 bit 地址。"""
        unit = self._require_unit(unit_id)
        space = self._bit_space(unit, register_type)
        return [space[address + offset] for offset in range(count)]

    def read_registers(
        self,
        unit_id: int,
        register_type: str,
        address: int,
        count: int = 1,
    ) -> list[int]:
        """读取已声明 16-bit register。"""
        unit = self._require_unit(unit_id)
        space = self._register_space(unit, register_type)
        return [space[address + offset] for offset in range(count)]

    @staticmethod
    def _require_declared(
        space: dict[int, bool] | dict[int, int],
        unit_id: int,
        register_type: str,
        addresses: list[int],
    ) -> None:
        for address in addresses:
            if address not in space:
                raise KeyError(f"Undeclared Modbus address {unit_id}/" f"{register_type}/{address}")

    def _require_unit(self, unit_id: int) -> ModbusUnitStore:
        try:
            return self._units[unit_id]
        except KeyError as exc:
            raise KeyError(f"Unknown Modbus unit_id {unit_id}") from exc

    @staticmethod
    def _bit_space(unit: ModbusUnitStore, register_type: str) -> dict[int, bool]:
        if register_type == "coil":
            return unit.coil
        if register_type == "discrete":
            return unit.discrete
        raise ValueError(f"Not a bit register_type: {register_type}")

    @staticmethod
    def _register_space(unit: ModbusUnitStore, register_type: str) -> dict[int, int]:
        if register_type == "holding":
            return unit.holding
        if register_type == "input":
            return unit.input
        raise ValueError(f"Not a word register_type: {register_type}")


__all__ = ["ModbusUnitStore", "ModbusSinkStore"]
