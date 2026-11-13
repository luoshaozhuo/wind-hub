"""One-device Modbus TCP Server backed by the authoritative Gateway variable store."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pymodbus.constants import ExcCodes
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simutils import SimUtils

from industrial_gateway.application.variable_store import (
    Scalar,
    VariableStore,
    validate_value,
)
from industrial_gateway.infrastructure.protocol.modbus.access import (
    GatewayTcpServer,
    request_bit_span,
)
from industrial_gateway.infrastructure.protocol.modbus.codec import (
    RegisterMapping,
    decode_registers,
    encode_registers,
)

logger = logging.getLogger(__name__)
_AREAS = {"coil": 0, "discrete_input": 1, "holding": 2, "input": 3}
_FUNCTION_AREAS = {
    1: "coil",
    2: "discrete_input",
    3: "holding",
    4: "input",
    5: "coil",
    6: "holding",
    15: "coil",
    16: "holding",
}
_WRITES = frozenset({5, 6, 15, 16})
WriteCallback = Callable[[str, Scalar], Awaitable[None]]


class DeviceModbusServer:
    """One listener, one Device, one Unit ID; no fallback multi-drop mapping."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        unit_id: int,
        mappings: tuple[RegisterMapping, ...],
        store: VariableStore,
        on_write: WriteCallback | None = None,
        allowed_writes: frozenset[str] | None = None,
    ) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError("Modbus Unit ID out of range")
        self._host = host
        self._port = port
        self._server: GatewayTcpServer | None = None
        self._on_write = on_write
        self._allowed_writes = allowed_writes
        self._store = store
        self._mappings = mappings
        blocks: list[list[SimData]] = [[], [], [], []]
        for mapping in mappings:
            is_bit = mapping.area in {"coil", "discrete_input"}
            blocks[_AREAS[mapping.area]].append(
                SimData(
                    address=mapping.address,
                    count=mapping.count,
                    values=False if is_bit else 0,
                    datatype=DataType.BITS if is_bit else DataType.REGISTERS,
                    readonly=mapping.area in {"input", "discrete_input"},
                )
            )
        for index, group in enumerate(blocks):
            if not group:
                bit_area = index < 2
                group.append(
                    SimData(
                        address=0,
                        values=False if bit_area else 0,
                        datatype=DataType.BITS if bit_area else DataType.INVALID,
                    )
                )
        self._device = SimDevice(
            id=unit_id,
            simdata=(blocks[0], blocks[1], blocks[2], blocks[3]),
            action=self._handle_access,
        )
        self._access_lock = asyncio.Lock()

    async def _handle_access(
        self,
        function_code: int,
        start_address: int,
        address: int,
        count: int,
        current_registers: list[int],
        set_values: list[int] | list[bool] | None,
    ) -> ExcCodes | None:
        if function_code not in _FUNCTION_AREAS:
            return ExcCodes.ILLEGAL_FUNCTION
        # FC05/FC06 perform a post-write echo read through the same action with
        # set_values=None, so the operation kind follows the payload presence,
        # never the function code alone.
        area = _FUNCTION_AREAS[function_code]
        is_write = function_code in _WRITES and set_values is not None
        is_bit = area in {"coil", "discrete_input"}
        if is_write:
            assert set_values is not None
            span = len(set_values)
            begin, end = address, address + span
        elif is_bit:
            requested = request_bit_span()
            begin, end = (
                (requested[0], requested[0] + requested[1])
                if requested is not None
                else (start_address * 16, (start_address + count) * 16)
            )
        else:
            begin, end = address, address + count

        relevant = sorted(
            (
                mapping
                for mapping in self._mappings
                if mapping.area == area
                and mapping.address < end
                and mapping.address + mapping.count > begin
            ),
            key=lambda mapping: mapping.address,
        )
        if not relevant:
            return ExcCodes.ILLEGAL_ADDRESS

        if is_write:
            # Accept complete writes, never partial logical values.
            writes = set_values
            assert writes is not None
            cursor = begin
            decoded: list[tuple[str, bool | int | float]] = []
            for mapping in relevant:
                if mapping.address != cursor:
                    return ExcCodes.ILLEGAL_ADDRESS
                offset = mapping.address - address
                raw_values = writes[offset : offset + mapping.count]
                try:
                    value = decode_registers(
                        mapping,
                        [int(item) for item in raw_values],
                    )
                    validate_value(mapping.data_type, value)
                except (TypeError, ValueError):
                    return ExcCodes.ILLEGAL_VALUE
                if not isinstance(value, bool | int | float):
                    return ExcCodes.ILLEGAL_VALUE
                decoded.append((mapping.point_id, value))
                cursor += mapping.count
            if cursor != end:
                return ExcCodes.ILLEGAL_ADDRESS
            if self._allowed_writes is not None and any(
                variable_id not in self._allowed_writes for variable_id, _ in decoded
            ):
                return ExcCodes.ILLEGAL_ADDRESS
            async with self._access_lock:
                for variable_id, value in decoded:
                    try:
                        await self._store.update(variable_id, value)
                    except (KeyError, ValueError, TypeError):
                        return ExcCodes.ILLEGAL_VALUE
                    if self._on_write is not None:
                        try:
                            await self._on_write(variable_id, value)
                        except Exception:
                            logger.exception("Modbus request-level write notification failed")
            return None

        if not is_bit:
            cursor = begin
            for mapping in relevant:
                if mapping.address != cursor:
                    return ExcCodes.ILLEGAL_ADDRESS
                cursor += mapping.count
            if cursor != end:
                return ExcCodes.ILLEGAL_ADDRESS
        updated = list(current_registers)
        bits = SimUtils.registersToBits(updated) if is_bit else None
        for mapping in relevant:
            stored: bool | int | float | str | bytes | None = self._store.read(
                mapping.point_id
            ).value
            if stored is None:
                stored = False if is_bit else 0
            elif not isinstance(stored, bool | int | float):
                return ExcCodes.DEVICE_FAILURE
            try:
                words = encode_registers(mapping, stored)
            except (TypeError, ValueError, OverflowError):
                return ExcCodes.DEVICE_FAILURE
            if bits is not None:
                index = mapping.address - start_address * 16
                if not 0 <= index < len(bits):
                    return ExcCodes.ILLEGAL_ADDRESS
                bits[index] = bool(words[0])
            else:
                index = mapping.address - start_address
                updated[index : index + mapping.count] = words
        current_registers[:] = SimUtils.bitsToRegisters(bits) if bits is not None else updated
        return None

    @property
    def is_serving(self) -> bool:
        """Report the actual pymodbus listener state without opening a connection."""
        server = self._server
        return server is not None and server.transport is not None and not server.serving.done()

    async def start(self) -> None:
        """Bind and start a real pymodbus TCP listener."""
        if self._server is not None:
            raise RuntimeError("Modbus listener already running")
        server = GatewayTcpServer(
            [self._device],
            address=(self._host, self._port),
            broadcast_enable=False,
            ignore_missing_devices=False,
        )
        self._server = server
        try:
            await server.serve_forever(background=True)
        except BaseException:
            # Retain ownership if shutdown itself fails; a recovery attempt must
            # not create a replacement listener before this one is released.
            await self.stop()
            raise

    async def stop(self) -> None:
        """Release the owned listener before reporting STOPPED."""
        server = self._server
        if server is None:
            return
        release_listener: Callable[[], Awaitable[None]] = server.shutdown
        await release_listener()
        self._server = None
