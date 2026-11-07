"""非侵入式工业协议观测 Wrapper：不反射或修改真实 Driver。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any, TypeVar

from core.application.port.protocol import (
    ListenHandle,
    ProtocolPort,
    ProtocolSampleCallback,
    SubscriptionHandle,
)
from core.application.protocol_contract import (
    ConnectionHealth,
    InterrogationRequest,
    PointTableUpdateResult,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)
from core.domain import PointTable

_T = TypeVar("_T")


class _ObservedSubscription:
    def __init__(self, inner: SubscriptionHandle, observe: Callable[..., Any]) -> None:
        self._inner = inner
        self._observe = observe

    async def start(self) -> None:
        await self._observe("subscription_start", self._inner.start)

    async def stop(self) -> None:
        await self._observe("subscription_stop", self._inner.stop)


class _ObservedListener:
    def __init__(self, inner: ListenHandle, observe: Callable[..., Any]) -> None:
        self._inner = inner
        self._observe = observe

    async def close(self) -> None:
        await self._observe("listener_close", self._inner.close)


class ObservedProtocolPort:
    """对公开端口显式委托，观测失败不改变 PLC 读写与异常。"""

    def __init__(
        self,
        driver: ProtocolPort[Any],
        *,
        protocol: str,
        record: Callable[[str, str, str, float], None],
    ) -> None:
        if protocol not in {"ads", "modbus", "iec104"}:
            raise ValueError("unknown industrial protocol")
        self._driver = driver
        self._protocol = protocol
        self._record = record

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._driver.capabilities()

    def is_open(self) -> bool:
        return self._driver.is_open()

    async def _observe(
        self,
        operation: str,
        call: Callable[[], Awaitable[_T]],
        outcome: Callable[[_T], str] | None = None,
    ) -> _T:
        started = time.monotonic()
        status = "success"
        try:
            response = await call()
            if outcome is not None:
                status = outcome(response)
            return response
        except BaseException:
            status = "failure"
            raise
        finally:
            # Optional exporter must not mutate the original protocol outcome.
            with suppress(Exception):
                self._record(self._protocol, operation, status, time.monotonic() - started)

    async def connect(self) -> None:
        await self._observe("connect", self._driver.connect)

    async def close(self) -> None:
        await self._observe("close", self._driver.close)

    async def health(self) -> ConnectionHealth:
        return await self._observe(
            "health",
            self._driver.health,
            lambda value: "success" if value.healthy else "failure",
        )

    async def read_one(self, point_id: str) -> ProtocolSample:
        return await self._observe("read_one", lambda: self._driver.read_one(point_id))

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        return await self._observe("read_many", lambda: self._driver.read_many(point_ids))

    @staticmethod
    def _write_status(result: ProtocolWriteResult) -> str:
        if result.outcome is WriteOutcome.UNKNOWN:
            return "unknown"
        return "success" if result.outcome is WriteOutcome.CONFIRMED_SUCCESS else "failure"

    async def write_one(self, write: ProtocolWrite[Any]) -> ProtocolWriteResult:
        return await self._observe(
            "write_one",
            lambda: self._driver.write_one(write),
            self._write_status,
        )

    async def write_many(
        self, writes: Sequence[ProtocolWrite[Any]]
    ) -> tuple[ProtocolWriteResult, ...]:
        def status(results: tuple[ProtocolWriteResult, ...]) -> str:
            states = {self._write_status(result) for result in results}
            if "unknown" in states:
                return "unknown"
            if "failure" in states:
                return "failure"
            return "success"

        return await self._observe("write_many", lambda: self._driver.write_many(writes), status)

    async def update_point_table(
        self, point_table: PointTable, *, timeout: float
    ) -> PointTableUpdateResult:
        return await self._observe(
            "update_point_table",
            lambda: self._driver.update_point_table(point_table, timeout=timeout),
            lambda result: "success"
            if result.status.value in {"SUCCESS", "UNCHANGED"}
            else "failure",
        )

    def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        # 构造句柄不产生网络 I/O；原生注册、回收由包装后的生命周期计量。
        handle = self._driver.subscribe(point_ids, callback, interval=interval)
        return _ObservedSubscription(handle, self._observe)

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        handle = await self._observe(
            "listen", lambda: self._driver.listen(point_ids, callback, interval=interval)
        )
        return _ObservedListener(handle, self._observe)

    async def interrogate(self, request: InterrogationRequest) -> tuple[ProtocolSample, ...]:
        return await self._observe("interrogate", lambda: self._driver.interrogate(request))
