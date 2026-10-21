"""基于 pymodbus 3.15 的 Modbus TCP 服务端，不实现任何 TCP/MBAP/PDU 解析。

当前接入仅允许授权的数据写入口更新反馈点；控制入口尚未启用时保持拒绝。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from pymodbus.constants import ExcCodes
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simutils import SimUtils

from industrial_gateway.application.write_service import (
    ResolvedWrite,
    WriteService,
    WriterRole,
)
from industrial_gateway.domain import PointSnapshot
from industrial_gateway.infrastructure.protocol.modbus.access import GatewayTcpServer, request_ip
from industrial_gateway.infrastructure.protocol.modbus.codec import (
    RegisterMap,
    RegisterMapping,
    decode_registers,
    encode_registers,
)
from industrial_gateway.application.point_store import PointStore

logger = logging.getLogger(__name__)

PointKey = tuple[str, str]
_AREAS = {"coil": 0, "discrete_input": 1, "holding": 2, "input": 3}
_READ_CODES = {1, 2, 3, 4}
_WRITE_CODES = {5, 6, 15, 16}


class PymodbusGatewayServer:
    """一个 Endpoint 对应多个 Unit ID，共享业务层的 Point Store。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        units: Mapping[int, list[RegisterMapping]],
        store: PointStore,
        writes: WriteService,
        data_writers: frozenset[str] = frozenset(),
        command_writers: frozenset[str] = frozenset(),
    ) -> None:
        self._host = host
        self._port = port
        self._server: GatewayTcpServer | None = None
        if data_writers & command_writers:
            raise ValueError("data and command writer IPs must not overlap")
        devices: list[SimDevice] = []
        for unit_id, mappings in units.items():
            if not 1 <= unit_id <= 247:
                raise ValueError("unit ID out of range")
            RegisterMap(mappings)
            blocks: tuple[list[SimData], list[SimData], list[SimData], list[SimData]] = (
                [], [], [], []
            )
            for mapping in mappings:
                is_bit = mapping.area in {"coil", "discrete_input"}
                block = SimData(
                    address=mapping.address,
                    count=mapping.count,
                    values=False if is_bit else 0,
                    datatype=DataType.BITS if is_bit else DataType.REGISTERS,
                    readonly=mapping.area in {"input", "discrete_input"},
                )
                blocks[_AREAS[mapping.area]].append(block)
            for block in blocks:
                if not block:
                    block.append(SimData(address=0, datatype=DataType.BITS if block in blocks[:2] else DataType.INVALID, values=False if block in blocks[:2] else 0))
            devices.append(
                SimDevice(
                    id=unit_id,
                    simdata=blocks,
                    action=self._action(mappings, store, writes, data_writers, command_writers),
                )
            )
        self._devices = devices

    @staticmethod
    def _action(
        mappings: list[RegisterMapping],
        store: PointStore,
        writes: WriteService,
        data_writers: frozenset[str],
        command_writers: frozenset[str],
    ):
        """工厂绑定 Unit 的映射，回调不读取全局可变请求上下文。"""

        async def on_access(
            function_code: int,
            start_address: int,
            address: int,
            count: int,
            current_registers: list[int],
            set_values: list[int] | list[bool] | None,
        ) -> ExcCodes | None:
            if function_code not in _READ_CODES | _WRITE_CODES:
                return ExcCodes.ILLEGAL_FUNCTION
            area = {
                1: "coil", 2: "discrete_input", 3: "holding", 4: "input",
                5: "coil", 6: "holding", 15: "coil", 16: "holding",
            }[function_code]
            write_request = function_code in _WRITE_CODES and set_values is not None
            span = len(set_values) if write_request and area in {"coil", "discrete_input"} else count
            relevant = [
                mapping for mapping in mappings
                if mapping.area == area
                and mapping.address < address + span
                and mapping.address + mapping.count > address
            ]
            if write_request:
                peer = request_ip()
                if peer is None or set_values is None:
                    return ExcCodes.ILLEGAL_ADDRESS
                writer_role = (
                    WriterRole.DATA if peer in data_writers else
                    WriterRole.COMMAND if peer in command_writers else None
                )
                if writer_role is None:
                    return ExcCodes.ILLEGAL_ADDRESS
                # 一次请求可完整覆盖多个相邻的反馈点，不支持跨越空洞/半点。
                decoded: dict[PointKey, bool | int | float] = {}
                expected = address
                for mapping in sorted(relevant, key=lambda item: item.address):
                    if mapping.address != expected:
                        return ExcCodes.ILLEGAL_ADDRESS
                    begin = mapping.address - address
                    end = begin + mapping.count
                    if end > len(set_values):
                        return ExcCodes.ILLEGAL_ADDRESS
                    try:
                        value = decode_registers(
                            mapping, [int(item) for item in set_values[begin:end]]
                        )
                    except (TypeError, ValueError):
                        return ExcCodes.ILLEGAL_VALUE
                    decoded[(mapping.device_id, mapping.point_id)] = value
                    expected += mapping.count
                if expected != address + len(set_values):
                    return ExcCodes.ILLEGAL_ADDRESS
                try:
                    await writes.write(ResolvedWrite(decoded, writer_role))
                except (TypeError, ValueError, PermissionError, KeyError):
                    return ExcCodes.ILLEGAL_ADDRESS
                except Exception:
                    logger.exception("Gateway feedback batch rejected")
                    return ExcCodes.DEVICE_FAILURE
                return None
            # bit 访问的 count 是底层 16-bit 寄存器数，而非请求 bit 数。
            is_bit = area in {"coil", "discrete_input"}
            if not is_bit:
                cursor = address
                for mapping in sorted(relevant, key=lambda item: item.address):
                    if mapping.address != cursor:
                        return ExcCodes.ILLEGAL_ADDRESS
                    cursor += mapping.count
                if cursor != address + count:
                    return ExcCodes.ILLEGAL_ADDRESS
            elif not relevant:
                return ExcCodes.ILLEGAL_ADDRESS

            updated_words = list(current_registers)
            bits = SimUtils.registersToBits(updated_words) if is_bit else None
            for mapping in relevant:
                snapshot: PointSnapshot = store.read(mapping.device_id, mapping.point_id)
                if snapshot.value is None:
                    return ExcCodes.DEVICE_FAILURE
                try:
                    encoded = encode_registers(mapping, snapshot.value)
                except (TypeError, ValueError, OverflowError):
                    return ExcCodes.DEVICE_FAILURE
                if bits is not None:
                    offset = mapping.address - start_address * 16
                    if not 0 <= offset < len(bits):
                        return ExcCodes.ILLEGAL_ADDRESS
                    bits[offset] = bool(encoded[0])
                else:
                    offset = mapping.address - start_address
                    updated_words[offset : offset + mapping.count] = encoded
            if bits is not None:
                updated_words = SimUtils.bitsToRegisters(bits)
            current_registers[:] = updated_words
            return None

        return on_access

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("server already started")
        server = GatewayTcpServer(
            self._devices,
            address=(self._host, self._port),
            broadcast_enable=False,
            ignore_missing_devices=False,
        )
        await server.serve_forever(background=True)
        self._server = server

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.shutdown()
