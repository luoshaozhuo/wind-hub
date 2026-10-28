"""Modbus Sink 内存数据区。

根据 ResolvedSinkPoint 预建 slave_id 下的 coil/discrete/holding/input 稀疏地址空间，
并接收 EncodedModbusValue 更新。该层不依赖 pymodbus，也不负责 TCP Server。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.application.sink_contract import ModbusSinkAddress, ResolvedSinkPoint
from core.infrastructure.config.sink_schema import MODBUS_WORD_WIDTH
from sink_service.infrastructure.adapters.modbus.codec import EncodedModbusValue


@dataclass(slots=True)
class ModbusSlaveStore:
    """单个 Modbus slave 的四类地址空间。"""

    coil: dict[int, bool] = field(default_factory=dict)
    discrete: dict[int, bool] = field(default_factory=dict)
    holding: dict[int, int] = field(default_factory=dict)
    input: dict[int, int] = field(default_factory=dict)


class ModbusSinkStore:
    """按 slave_id 组织的 Modbus Sink 稀疏内存模型。"""

    def __init__(self, points: list[ResolvedSinkPoint]) -> None:
        self._slaves: dict[int, ModbusSlaveStore] = {}
        for point in points:
            address = point.address
            if not isinstance(address, ModbusSinkAddress):
                continue
            slave = self._slaves.setdefault(address.slave_id, ModbusSlaveStore())
            if address.register_type in {"coil", "discrete"}:
                self._bit_space(slave, address.register_type)[address.address] = False
                continue

            width = MODBUS_WORD_WIDTH[point.datatype]
            registers = self._register_space(slave, address.register_type)
            for offset in range(width):
                registers[address.address + offset] = 0

    @property
    def slave_ids(self) -> tuple[int, ...]:
        """返回已声明 slave_id，按升序排列。"""
        return tuple(sorted(self._slaves))

    def write(self, value: EncodedModbusValue) -> None:
        """把已编码值写入预声明地址空间。"""
        slave = self._slaves.get(value.slave_id)
        if slave is None:
            raise KeyError(f"Unknown Modbus slave_id {value.slave_id}")

        if value.register_type in {"coil", "discrete"}:
            bit_space = self._bit_space(slave, value.register_type)
            if value.address not in bit_space:
                raise KeyError(
                    f"Undeclared Modbus address {value.slave_id}/"
                    f"{value.register_type}/{value.address}"
                )
            addresses = [value.address + offset for offset in range(len(value.bits))]
            self._require_declared(
                bit_space,
                value.slave_id,
                value.register_type,
                addresses,
            )
            for address, bit in zip(addresses, value.bits, strict=True):
                bit_space[address] = bit
            return

        register_space = self._register_space(slave, value.register_type)
        addresses = [value.address + offset for offset in range(len(value.registers))]
        self._require_declared(
            register_space,
            value.slave_id,
            value.register_type,
            addresses,
        )
        for address, register in zip(addresses, value.registers, strict=True):
            register_space[address] = register

    def read_bits(
        self,
        slave_id: int,
        register_type: str,
        address: int,
        count: int = 1,
    ) -> list[bool]:
        """读取已声明 bit 地址。"""
        slave = self._require_slave(slave_id)
        space = self._bit_space(slave, register_type)
        return [space[address + offset] for offset in range(count)]

    def read_registers(
        self,
        slave_id: int,
        register_type: str,
        address: int,
        count: int = 1,
    ) -> list[int]:
        """读取已声明 16-bit register。"""
        slave = self._require_slave(slave_id)
        space = self._register_space(slave, register_type)
        return [space[address + offset] for offset in range(count)]

    @staticmethod
    def _require_declared(
        space: dict[int, bool] | dict[int, int],
        slave_id: int,
        register_type: str,
        addresses: list[int],
    ) -> None:
        for address in addresses:
            if address not in space:
                raise KeyError(f"Undeclared Modbus address {slave_id}/{register_type}/{address}")

    def _require_slave(self, slave_id: int) -> ModbusSlaveStore:
        try:
            return self._slaves[slave_id]
        except KeyError as exc:
            raise KeyError(f"Unknown Modbus slave_id {slave_id}") from exc

    @staticmethod
    def _bit_space(slave: ModbusSlaveStore, register_type: str) -> dict[int, bool]:
        if register_type == "coil":
            return slave.coil
        if register_type == "discrete":
            return slave.discrete
        raise ValueError(f"Not a bit register_type: {register_type}")

    @staticmethod
    def _register_space(slave: ModbusSlaveStore, register_type: str) -> dict[int, int]:
        if register_type == "holding":
            return slave.holding
        if register_type == "input":
            return slave.input
        raise ValueError(f"Not a word register_type: {register_type}")


__all__ = ["ModbusSlaveStore", "ModbusSinkStore"]
