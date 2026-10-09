"""Protocol recovery contract: reconnect before I/O, never resend controls."""

from __future__ import annotations

import asyncio

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.application.recovery import RecoveryPort, RecoverySettings


class _Driver:
    def __init__(self, *, fail_connect: int = 0) -> None:
        self.connected = False
        self.fail_connect = fail_connect
        self.connect_count = 0
        self.read_count = 0
        self.write_count = 0
        self.drop_during_read = False
        self.drop_during_write = False

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self.connected)

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
            }
        )

    async def connect(self) -> None:
        self.connect_count += 1
        if self.connect_count <= self.fail_connect:
            raise ProtocolError("unreachable")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def _read(
        self, point_ids: tuple[str, ...]
    ) -> tuple[ProtocolSample, ...]:
        self.read_count += 1
        if self.drop_during_read and self.read_count == 1:
            self.connected = False
            raise ProtocolError("connection lost")
        return tuple(ProtocolSample(point_id=p, value=42, quality=Quality.GOOD)
                     for p in point_ids)

    async def read_one(self, point_id: str) -> ProtocolSample:
        return (await self._read((point_id,)))[0]

    async def read_many(self, point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        return await self._read(tuple(point_ids))

    async def _write(
        self, writes: tuple[ProtocolWrite, ...]
    ) -> tuple[ProtocolWriteResult, ...]:
        self.write_count += 1
        if self.drop_during_write:
            self.connected = False
            raise ProtocolError("lost after send")
        return tuple(ProtocolWriteResult(point_id=w.point_id, success=True)
                     for w in writes)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        return (await self._write((write,)))[0]

    async def write_many(
        self, writes: tuple[ProtocolWrite, ...]
    ) -> tuple[ProtocolWriteResult, ...]:
        return await self._write(tuple(writes))


@pytest.mark.asyncio
async def test_read_recovers_with_one_default_attempt() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings())
    values = await port.read_many(("a",))
    assert values[0].value == 42
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_retries_connection_up_to_configured_count() -> None:
    driver = _Driver(fail_connect=2)
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=3))
    assert (await port.read_one("a")).value == 42
    assert driver.connect_count == 3


@pytest.mark.asyncio
async def test_disabled_recovery_does_not_try_connect() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=0))
    with pytest.raises(ProtocolError, match="0 reconnect"):
        await port.read_one("a")
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_read_retries_after_detected_disconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_read = True
    port = RecoveryPort(driver, RecoverySettings())
    assert (await port.read_one("a")).value == 42
    assert driver.read_count == 2
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_write_is_not_resent_after_disconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_write = True
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=3))
    with pytest.raises(ProtocolError, match="lost after send"):
        await port.write_one(ProtocolWrite("control", 1))
    assert driver.write_count == 1
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_write_connects_before_first_send() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=1))
    result = await port.write_one(ProtocolWrite("control", 1))
    assert result.success
    assert driver.connect_count == 1
    assert driver.write_count == 1


@pytest.mark.asyncio
async def test_concurrent_reads_share_single_reconnect() -> None:
    """并发请求同时发现断线：重连互斥，只建立一次连接。"""
    driver = _Driver()

    async def slow_connect() -> None:
        driver.connect_count += 1
        await asyncio.sleep(0.01)
        driver.connected = True

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))
    results = await asyncio.gather(*(port.read_one(f"p{i}") for i in range(8)))
    assert all(sample.value == 42 for sample in results)
    assert driver.connect_count == 1


def test_recovery_configuration_rejects_invalid_attempts() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        RecoverySettings(reconnect_attempts=-1)
    with pytest.raises(ValueError, match="nonnegative"):
        RecoverySettings(reconnect_attempts=True)


@pytest.mark.asyncio
async def test_read_timeout_is_enforced() -> None:
    driver = _Driver()
    driver.connected = True

    async def slow_read(point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        del point_ids
        await asyncio.sleep(0.05)
        return ()

    driver.read_many = slow_read  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(read_timeout=0.001))
    with pytest.raises(ProtocolError, match="timed out"):
        await port.read_many(("a",))


@pytest.mark.asyncio
async def test_write_timeout_does_not_replay_command() -> None:
    driver = _Driver()
    driver.connected = True

    async def slow_write(write: ProtocolWrite) -> ProtocolWriteResult:
        del write
        driver.write_count += 1
        await asyncio.sleep(0.05)
        return ProtocolWriteResult(point_id="control", success=True)

    driver.write_one = slow_write  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(write_timeout=0.001))
    with pytest.raises(ProtocolError, match="write timed out"):
        await port.write_one(ProtocolWrite("control", 5))
    assert driver.write_count == 1


@pytest.mark.asyncio
async def test_unsupported_write_many_propagates_without_fallback() -> None:
    """Driver 明确不支持 write_many 时：原样抛出，不降级为逐点写入。"""

    class _NoBatchDriver(_Driver):
        def capabilities(self) -> frozenset[ProtocolCapability]:
            return frozenset({ProtocolCapability.READ, ProtocolCapability.WRITE})

        async def write_many(
            self, writes: tuple[ProtocolWrite, ...]
        ) -> tuple[ProtocolWriteResult, ...]:
            raise NotImplementedError("write_many is not implemented")

    driver = _NoBatchDriver()
    driver.connected = True
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=3))
    with pytest.raises(NotImplementedError):
        await port.write_many((ProtocolWrite("control", 1),))
    with pytest.raises(NotImplementedError):
        await port.write_many(())
    assert driver.write_count == 0
    assert driver.connect_count == 0  # 已连接时不做任何额外动作

    # 断线状态下同样在重连之前拒绝，不做无意义的连接恢复。
    driver.connected = False
    with pytest.raises(NotImplementedError):
        await port.write_many((ProtocolWrite("control", 1),))
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_connection_timeout_is_enforced() -> None:
    driver = _Driver()

    async def slow_connect() -> None:
        await asyncio.sleep(0.05)

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(connect_timeout=0.001))
    with pytest.raises(ProtocolError, match="connect timed out"):
        await port.connect()
