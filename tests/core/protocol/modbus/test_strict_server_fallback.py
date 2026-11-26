"""真实严格 Modbus 服务端上的稀疏点表回退、缓存与异常分流测试。"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator

import pytest

from core.application.errors import ProtocolConnectionError, ProtocolError
from core.application.protocol_contract import PointTableUpdateStatus, Quality
from core.domain import ConnectionEndpoint, Point, PointAccess, PointTable, Protocol
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.session import _ModbusIllegalDataAddressError
from tests.support.process import free_port
from tests.support.units import NONE_UNIT


def _point(point_id: str, address: int, register_type: str = "holding") -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ,
        ext={
            "address": address,
            "register_type": register_type,
            "data_type": "uint16",
            "slave_id": 1,
        },
    )


def _table(table_id: str, points: dict[str, int], register_type: str = "holding") -> PointTable:
    return PointTable(
        PointTableId(table_id),
        Protocol("modbus"),
        {
            point_id: _point(point_id, address, register_type)
            for point_id, address in points.items()
        },
    )


def _driver(
    port: int,
    points: dict[str, int],
    *,
    timeout: float = 1.0,
    register_type: str = "holding",
) -> ModbusDriver:
    return ModbusDriver(
        ConnectionEndpoint("127.0.0.1", port),
        _table("sparse", points, register_type),
        {"timeout": timeout},
    )


class _WireServer:
    """A real TCP Modbus responder whose address holes are strictly rejected.

    It speaks the Modbus TCP framing directly so the production session,
    client and driver are exercised without replacing them with mocks.
    """

    def __init__(self, *, mode: str = "allowed", defined: set[int] | None = None) -> None:
        self.mode = mode
        self.defined = defined
        self.requests: list[tuple[int, int, int]] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> _WireServer:
        self.port = free_port()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *_: object) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    def _exception_code(self, function: int, start: int, quantity: int) -> int | None:
        if self.mode == "code1":
            return 1
        if self.mode == "code3":
            return 3
        if self.defined is not None and not all(
            address in self.defined for address in range(start, start + quantity)
        ):
            return 2
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                pdu = await reader.readexactly(5)
                function = pdu[0]
                start, quantity = struct.unpack(">HH", pdu[1:5])
                self.requests.append((function, start, quantity))
                if self.mode == "disconnect":
                    return
                if self.mode == "timeout":
                    await asyncio.sleep(2.0)
                    return
                if self.mode == "wrong_function":
                    response_function = 3 if function == 4 else 4
                else:
                    response_function = function
                code = self._exception_code(function, start, quantity)
                if code is not None:
                    payload = bytes([response_function | 0x80, code])
                else:
                    extra = 1 if self.mode == "extra_word" else 0
                    words = b"".join(
                        struct.pack(">H", address + 100)
                        for address in range(start, start + quantity + extra)
                    )
                    payload = bytes([response_function, 2 * (quantity + extra)]) + words
                writer.write(
                    header[0:2]
                    + b"\x00\x00"
                    + struct.pack(">H", len(payload) + 1)
                    + bytes([header[6]])
                    + payload
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@contextlib.asynccontextmanager
async def _pymodbus_sparse_server(values: dict[int, int]) -> AsyncIterator[int]:
    """Real PyModbus server whose sparse datastore rejects unconfigured holes."""
    from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusServerContext,
        ModbusSparseDataBlock,
    )
    from pymodbus.server import ModbusTcpServer

    port = free_port()
    context = ModbusServerContext(
        devices={1: ModbusDeviceContext(hr=ModbusSparseDataBlock(values))}, single=False
    )
    server = ModbusTcpServer(context, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    try:
        yield port
    finally:
        with contextlib.suppress(Exception):
            await server.shutdown()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_real_pymodbus_sparse_server_rejects_gap_and_driver_recovers() -> None:
    """生产 PyModbus 稀疏从站返回 Exception Code 02 时按连续地址恢复。"""
    async with _pymodbus_sparse_server({0: 100, 6: 106}) as port:
        driver = _driver(port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            samples = await driver.read_many(["first", "second"])
            assert [sample.value for sample in samples] == [100, 106]
            assert all(sample.quality is Quality.GOOD for sample in samples)
            key = ("first", "second")
            plan = [[point.point_id for point in group] for group in driver._read_plan_cache[key]]
            assert plan == [["first"], ["second"]]
            assert [sample.value for sample in await driver.read_many(["first", "second"])] == [
                100,
                106,
            ]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_wire_server_gap_rejection_splits_once_and_caches_plan() -> None:
    """合并请求失败后只拆分一次；缓存后不再重复发送非法合并读取。"""
    async with _WireServer(defined={0, 6}) as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            samples = await driver.read_many(["first", "second"])
            assert [sample.value for sample in samples] == [100, 106]
            assert all(sample.quality is Quality.GOOD for sample in samples)
            assert all(sample.timestamp.tzinfo is not None for sample in samples)
            assert server.requests == [(3, 0, 7), (3, 0, 1), (3, 6, 1)]
            second = await driver.read_many(["first", "second"])
            assert [sample.value for sample in second] == [100, 106]
            assert server.requests.count((3, 0, 7)) == 1
            assert server.requests[-2:] == [(3, 0, 1), (3, 6, 1)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_wire_server_allowing_gaps_keeps_single_merged_request() -> None:
    """服务器允许跨空洞读取时保留原批量优化，不得无条件拆分。"""
    async with _WireServer() as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            samples = await driver.read_many(["first", "second"])
            assert [sample.value for sample in samples] == [100, 106]
            assert server.requests == [(3, 0, 7)]
        finally:
            await driver.close()


@pytest.mark.parametrize("mode", ["code1", "code3"])
@pytest.mark.asyncio
async def test_non_address_modbus_exceptions_are_never_split(mode: str) -> None:
    """IllegalFunction/IllegalDataValue 必须保持失败可见，不得触发拆分。"""
    async with _WireServer(mode=mode) as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            with pytest.raises(ProtocolError) as raised:
                await driver.read_many(["first", "second"])
            assert not isinstance(raised.value, _ModbusIllegalDataAddressError)
            assert server.requests == [(3, 0, 7)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_transport_disconnect_does_not_retry_gap() -> None:
    """TCP 中断属于连接错误，不能伪装成地址拒绝并重发。"""
    async with _WireServer(mode="disconnect") as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            with pytest.raises(ProtocolConnectionError):
                await driver.read_many(["first", "second"])
            assert server.requests == [(3, 0, 7)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_timeout_does_not_retry_gap() -> None:
    """超时不得触发拆分重试，避免未知状态下额外 PLC 请求。"""
    async with _WireServer(mode="timeout") as server:
        driver = _driver(server.port, {"first": 0, "second": 6}, timeout=0.5)
        await driver.connect()
        try:
            with pytest.raises(ProtocolConnectionError):
                await driver.read_many(["first", "second"])
            assert server.requests == [(3, 0, 7)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_response_length_mismatch_does_not_retry() -> None:
    """返回长度与请求不一致属于连接错误，不得触发拆分重试。"""
    async with _WireServer(mode="extra_word") as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            with pytest.raises(ProtocolConnectionError):
                await driver.read_many(["first", "second"])
            assert server.requests == [(3, 0, 7)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_mismatched_function_code_does_not_retry() -> None:
    """功能码响应不匹配必须使会话失效，不得按地址拒绝拆分。"""
    async with _WireServer(mode="wrong_function") as server:
        driver = _driver(server.port, {"first": 0, "second": 6}, register_type="input")
        await driver.connect()
        try:
            with pytest.raises(ProtocolConnectionError):
                await driver.read_many(["first", "second"])
            assert server.requests == [(4, 0, 7)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_repeated_illegal_address_after_split_does_not_return_partial_success() -> None:
    """部分分组成功后仍失败时，整次读取必须失败而不是返回部分 GOOD。"""
    async with _WireServer(defined={0}) as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            with pytest.raises(_ModbusIllegalDataAddressError):
                await driver.read_many(["first", "second"])
            assert server.requests == [(3, 0, 7), (3, 0, 1), (3, 6, 1)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_persistent_address_rejection_evicts_invalid_cached_plan() -> None:
    """失败的跨空洞合并计划不得留存；下一周期只允许读取连续区间。"""
    async with _WireServer(defined={0}) as server:
        driver = _driver(server.port, {"first": 0, "second": 6})
        await driver.connect()
        try:
            with pytest.raises(_ModbusIllegalDataAddressError):
                await driver.read_many(["first", "second"])
            key = ("first", "second")
            assert [
                [point.point_id for point in group]
                for group in driver._read_plan_cache[key]
            ] == [["first"], ["second"]]
            assert server.requests == [(3, 0, 7), (3, 0, 1), (3, 6, 1)]

            with pytest.raises(_ModbusIllegalDataAddressError):
                await driver.read_many(["first", "second"])
            assert server.requests == [
                (3, 0, 7), (3, 0, 1), (3, 6, 1), (3, 0, 1), (3, 6, 1)
            ]
            assert server.requests.count((3, 0, 7)) == 1
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_online_update_failure_keeps_previous_point_table() -> None:
    """在线点表验证失败时旧表继续生效，缓存不得带入未验证分组。"""
    async with _WireServer(defined={0, 1}) as server:
        driver = _driver(server.port, {"a": 0, "b": 1})
        await driver.connect()
        try:
            result = await driver.update_point_table(_table("after", {"a": 3, "b": 9}), timeout=3.0)
            assert result.status is PointTableUpdateStatus.VERIFICATION_FAILED
            assert result.applied is False
            assert driver._point_table.point_table_id == "sparse"
            samples = await driver.read_many(["a", "b"])
            assert [sample.value for sample in samples] == [100, 101]
            assert server.requests[-1] == (3, 0, 2)
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_online_update_of_sparse_table_uses_same_fallback() -> None:
    """在线点表更新校验在严格服务器上同样按连续地址分组。"""
    async with _WireServer(defined={0, 1, 3, 9}) as server:
        driver = _driver(server.port, {"a": 0, "b": 1})
        await driver.connect()
        try:
            result = await driver.update_point_table(_table("after", {"a": 3, "b": 9}), timeout=3.0)
            assert result.status is PointTableUpdateStatus.SUCCESS
            assert result.applied is True
            assert server.requests == [(3, 3, 7), (3, 3, 1), (3, 9, 1)]
        finally:
            await driver.close()


@pytest.mark.asyncio
async def test_concurrent_reads_keep_independent_safe_plans() -> None:
    """并发稀疏读取不得互相污染读取计划缓存。"""
    async with _WireServer(defined={0, 6, 10, 16}) as server:
        driver = _driver(server.port, {"a": 0, "b": 6, "c": 10, "d": 16})
        await driver.connect()
        try:
            first, second = await asyncio.gather(
                driver.read_many(["a", "b"]), driver.read_many(["c", "d"])
            )
            assert [sample.value for sample in first] == [100, 106]
            assert [sample.value for sample in second] == [110, 116]
            for key, expected in (
                (("a", "b"), [["a"], ["b"]]),
                (("c", "d"), [["c"], ["d"]]),
            ):
                plan = [
                    [point.point_id for point in group] for group in driver._read_plan_cache[key]
                ]
                assert plan == expected
        finally:
            await driver.close()
