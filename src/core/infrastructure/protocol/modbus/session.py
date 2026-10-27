"""一次性 Modbus TCP 会话；由 PyModbus 负责 TID 与收包关联。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from pymodbus.client import AsyncModbusTcpClient

from core.application.errors import ProtocolConnectionError, ProtocolError
from core.application.protocol_contract import WriteOutcome

from .config import ModbusConfig

_T = TypeVar("_T")


class _ModbusSession:
    """独占一个客户端；不对正常读写增加外部串行锁。"""

    def __init__(self, config: ModbusConfig) -> None:
        self._client = AsyncModbusTcpClient(
            config.host,
            port=config.port,
            timeout=config.timeout,
            retries=0,
            reconnect_delay=0.0,
        )
        self._closing = False
        self._closed = False
        self._active: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None

    def is_open(self) -> bool:
        return not self._closing and self._client.connected

    @property
    def closed(self) -> bool:
        return self._closed

    async def open(self) -> None:
        async def connect_client() -> None:
            if not await self._client.connect():
                raise ProtocolConnectionError("Modbus TCP connection refused")

        task = asyncio.create_task(connect_client())
        self._active.add(task)

        def settle(done: asyncio.Task[None]) -> None:
            self._active.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settle)
        try:
            await asyncio.shield(task)
        except BaseException:
            # 建链被取消后仍由 Session 追踪真实结果，随后再关闭资源。
            self.invalidate()
            await asyncio.shield(self._start_close())
            raise

    async def _execute(self, request: Callable[[AsyncModbusTcpClient], Awaitable[_T]]) -> _T:
        """跟踪协议事务，取消调用方时继续收敛底层请求。"""

        # Task 保存底层事务所有权；取消调用方不会误认为网络请求已停止。
        async def invoke() -> _T:
            # 任务排队期间发生 close/cancel 时，不允许迟到提交请求。
            if not self.is_open():
                raise ProtocolConnectionError("Modbus session closed before submission")
            return await request(self._client)

        task = asyncio.create_task(invoke())
        self._active.add(task)

        def settle(_: asyncio.Future[_T]) -> None:
            self._active.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(settle)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 请求可能已提交：旧会话不可复用，禁止自动重试。
            self.invalidate()
            raise
        except Exception as exc:
            # 业务异常响应以响应对象返回；没有收到可信响应属于连接错误。
            self.invalidate()
            raise ProtocolConnectionError(f"Modbus transaction failed: {exc}") from exc

    async def read(
        self, register_type: str, address: int, count: int, slave_id: int
    ) -> Sequence[object]:
        """执行一次 FC01～FC04，并校验响应结构。"""

        async def request(client: AsyncModbusTcpClient) -> Any:
            if register_type == "coil":
                return await client.read_coils(address, count=count, device_id=slave_id)
            if register_type == "discrete_input":
                return await client.read_discrete_inputs(address, count=count, device_id=slave_id)
            if register_type == "holding":
                return await client.read_holding_registers(address, count=count, device_id=slave_id)
            if register_type == "input":
                return await client.read_input_registers(address, count=count, device_id=slave_id)
            raise ValueError(f"unsupported Modbus read area: {register_type}")

        response = await self._execute(request)
        expected = {"coil": 1, "discrete_input": 2, "holding": 3, "input": 4}[register_type]
        if response.isError():
            raise ProtocolError(f"Modbus {register_type} read exception response")
        if getattr(response, "function_code", None) != expected:
            self.invalidate()
            raise ProtocolConnectionError("Modbus read function code mismatch")
        field = "bits" if register_type in ("coil", "discrete_input") else "registers"
        raw = getattr(response, field, None)
        if not isinstance(raw, (list, tuple)) or len(raw) < count:
            self.invalidate()
            raise ProtocolConnectionError("Modbus read response incomplete")
        return raw

    async def write(
        self,
        register_type: str,
        address: int,
        values: Sequence[int | bool],
        slave_id: int,
    ) -> tuple[WriteOutcome, str | None]:
        """执行单个 FC05/06/15/16 事务；未知结果不得自动重发。"""
        count = len(values)
        if not count or register_type not in ("coil", "holding"):
            raise ValueError("invalid Modbus write request")
        expected = (
            5
            if register_type == "coil" and count == 1
            else 15
            if register_type == "coil"
            else 6
            if count == 1
            else 16
        )

        async def request(client: AsyncModbusTcpClient) -> Any:
            if expected == 5:
                return await client.write_coil(address, bool(values[0]), device_id=slave_id)
            if expected == 15:
                return await client.write_coils(
                    address, [bool(value) for value in values], device_id=slave_id
                )
            if expected == 6:
                return await client.write_register(address, int(values[0]), device_id=slave_id)
            return await client.write_registers(
                address, [int(value) for value in values], device_id=slave_id
            )

        try:
            response = await self._execute(request)
        except asyncio.CancelledError:
            raise
        except ProtocolConnectionError as exc:
            return WriteOutcome.UNKNOWN, str(exc)

        if response.isError():
            code = getattr(response, "exception_code", None)
            outcome = WriteOutcome.CONFIRMED_FAILURE if code in (1, 2, 3) else WriteOutcome.UNKNOWN
            return outcome, f"Modbus exception code: {code}"
        if (
            getattr(response, "function_code", None) != expected
            or getattr(response, "address", None) != address
        ):
            self.invalidate()
            return WriteOutcome.UNKNOWN, "Modbus write response mismatch"
        if expected in (15, 16):
            valid = getattr(response, "count", None) == count
        elif expected == 5:
            valid = getattr(response, "bits", None) == [bool(values[0])]
        else:
            valid = getattr(response, "registers", None) == [int(values[0])]
        if not valid:
            self.invalidate()
            return WriteOutcome.UNKNOWN, "Modbus write echo mismatch"
        return WriteOutcome.CONFIRMED_SUCCESS, None

    def invalidate(self) -> None:
        self._closing = True
        self._start_close()

    def _start_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            task = asyncio.create_task(self._close())

            def settle(done: asyncio.Task[None]) -> None:
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(settle)
            self._close_task = task
        return self._close_task

    async def close(self) -> None:
        self._closing = True
        await asyncio.shield(self._start_close())

    async def _close(self) -> None:
        if self._closed:
            return
        self._closing = True
        # 原生 API 的在途调用由自身 Task 收敛；不提前丢失所有权。
        while self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
        self._client.close()
        self._closed = True
