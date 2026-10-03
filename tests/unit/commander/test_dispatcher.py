"""Commander CommandDispatcher 单元测试。

覆盖进程内幂等语义的四个核心承诺：

1. 相同 command_id 的并发请求共享一次真实设备写入（inflight 去重）；
2. 已完成命令在 TTL 内命中结果缓存，不再触发设备写；
3. 缓存 LRU 容量与 TTL 过期语义；
4. 写超时与设备异常收敛为 ``CommandResult(success=False)``，不向上抛。

Runtime 以最小 fake 替代——本层只验证 Dispatcher 自身逻辑，设备会话行为
由 component/integration 层覆盖。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from wind_hub_commander.dispatcher import CommandDispatcher
from wind_hub_core.model.command import Command, CommandResult


class _FakeDevice:
    """记录写入次数、可编程行为的最小设备会话。"""

    def __init__(self) -> None:
        self.write_calls: list[list[Command]] = []
        self.delay_s = 0.0
        self.error: Exception | None = None

    async def write(self, commands: list[Command]) -> list[CommandResult]:
        self.write_calls.append(commands)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return [
            CommandResult(command_id=command.command_id, success=True)
            for command in commands
        ]


class _FakeRuntime:
    """CommanderRuntime 的最小替代：单设备、operation 透传。"""

    def __init__(self, device: _FakeDevice, device_id: str = "d1") -> None:
        self._device = device
        self._device_id = device_id

    def device(self, device_id: str) -> _FakeDevice:
        if device_id != self._device_id:
            raise KeyError(f"unknown device '{device_id}'")
        return self._device

    async def ensure_connected(self, device_id: str) -> bool:
        self.device(device_id)
        return True

    def operation(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _noop():
            yield

        return _noop()


def _command(command_id: str, device_id: str = "d1", timeout: float = 0.0) -> Command:
    return Command(
        command_id=command_id,
        device_id=device_id,
        point_id="p1",
        value=1.0,
        timeout=timeout,
    )


@pytest.fixture
def device() -> _FakeDevice:
    return _FakeDevice()


@pytest.fixture
def dispatcher(device: _FakeDevice) -> CommandDispatcher:
    return CommandDispatcher(_FakeRuntime(device), default_timeout=1.0)  # type: ignore[arg-type]


class TestConcurrentDeduplication:
    async def test_concurrent_same_command_id_shares_single_write(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        device.delay_s = 0.05
        results = await asyncio.gather(
            *(dispatcher.send(_command("cmd-x")) for _ in range(8))
        )
        assert all(r.success for r in results)
        assert len(device.write_calls) == 1

    async def test_different_command_ids_each_execute(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        results = await asyncio.gather(
            *(dispatcher.send(_command(f"cmd-{i}")) for i in range(4))
        )
        assert all(r.success for r in results)
        assert len(device.write_calls) == 4


class TestResultCache:
    async def test_completed_command_hits_cache(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        first = await dispatcher.send(_command("cmd-cache"))
        assert first.success
        repeat = await dispatcher.send(_command("cmd-cache"))
        assert repeat.success
        assert len(device.write_calls) == 1

    async def test_failed_result_is_also_cached(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        """失败结果同样幂等——重试语义由调用方换 command_id 表达。"""
        device.error = RuntimeError("boom")
        first = await dispatcher.send(_command("cmd-fail"))
        assert not first.success
        device.error = None
        repeat = await dispatcher.send(_command("cmd-fail"))
        assert not repeat.success
        assert len(device.write_calls) == 1

    async def test_cache_evicts_oldest_beyond_capacity(self, device: _FakeDevice) -> None:
        dispatcher = CommandDispatcher(
            _FakeRuntime(device), default_timeout=1.0, idempotency_cache_size=4  # type: ignore[arg-type]
        )
        for i in range(4):
            await dispatcher.send(_command(f"cmd-{i}"))
        await dispatcher.send(_command("cmd-overflow"))
        # 容量 4 + 1 条新命令——最早写入的 cmd-0 必须被逐出。
        assert "cmd-0" not in dispatcher._cache
        assert "cmd-3" in dispatcher._cache
        assert "cmd-overflow" in dispatcher._cache

    async def test_cache_expires_after_ttl(self, device: _FakeDevice) -> None:
        dispatcher = CommandDispatcher(
            _FakeRuntime(device), default_timeout=1.0, idempotency_ttl=0.05  # type: ignore[arg-type]
        )
        await dispatcher.send(_command("cmd-ttl"))
        assert "cmd-ttl" in dispatcher._cache
        await asyncio.sleep(0.08)
        dispatcher._expire(time.monotonic())
        assert "cmd-ttl" not in dispatcher._cache
        await dispatcher.send(_command("cmd-ttl"))
        assert len(device.write_calls) == 2


class TestFailureSemantics:
    async def test_unknown_device_returns_failed_result(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        result = await dispatcher.send(_command("cmd-unknown", device_id="ghost"))
        assert not result.success
        assert "ghost" in (result.error or "")
        assert not device.write_calls

    async def test_device_exception_converges_to_result(
        self, dispatcher: CommandDispatcher
    ) -> None:
        class _BoomDevice(_FakeDevice):
            async def write(self, commands):  # type: ignore[override]
                raise ValueError("device exploded")

        dispatcher = CommandDispatcher(
            _FakeRuntime(_BoomDevice()), default_timeout=1.0  # type: ignore[arg-type]
        )
        result = await dispatcher.send(_command("cmd-boom"))
        assert not result.success
        assert "device exploded" in (result.error or "")

    async def test_write_timeout_converges_to_result(self, device: _FakeDevice) -> None:
        device.delay_s = 0.5
        dispatcher = CommandDispatcher(
            _FakeRuntime(device), default_timeout=0.05  # type: ignore[arg-type]
        )
        result = await dispatcher.send(_command("cmd-slow"))
        assert not result.success
        assert "timeout" in (result.error or "")

    async def test_command_timeout_overrides_default(self, device: _FakeDevice) -> None:
        device.delay_s = 0.2
        dispatcher = CommandDispatcher(
            _FakeRuntime(device), default_timeout=0.05  # type: ignore[arg-type]
        )
        result = await dispatcher.send(_command("cmd-override", timeout=1.0))
        assert result.success

    async def test_empty_device_response_converges_to_result(
        self, dispatcher: CommandDispatcher, device: _FakeDevice, monkeypatch
    ) -> None:
        async def _empty(commands):
            return []

        monkeypatch.setattr(device, "write", _empty)
        result = await dispatcher.send(_command("cmd-empty"))
        assert not result.success
        assert "empty" in (result.error or "")

    async def test_inflight_entry_cleaned_after_completion(
        self, dispatcher: CommandDispatcher
    ) -> None:
        """inflight 表不得泄漏——完成后同 id 新请求走缓存而非悬挂 task。"""
        await dispatcher.send(_command("cmd-clean"))
        await asyncio.sleep(0)
        assert not dispatcher._inflight


class TestIdempotencyConflict:
    async def test_completed_id_reused_with_different_payload_is_rejected(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        first = await dispatcher.send(_command("cmd-conflict"))
        conflict = await dispatcher.send(
            Command(command_id="cmd-conflict", device_id="d1", point_id="p1", value=2.0)
        )
        assert first.success
        assert not conflict.success
        assert "idempotency conflict" in (conflict.error or "")
        assert len(device.write_calls) == 1

    async def test_inflight_id_reused_with_different_payload_is_rejected(
        self, dispatcher: CommandDispatcher, device: _FakeDevice
    ) -> None:
        device.delay_s = 0.05
        first_task = asyncio.create_task(dispatcher.send(_command("cmd-live")))
        await asyncio.sleep(0)
        conflict = await dispatcher.send(
            Command(command_id="cmd-live", device_id="d1", point_id="p1", value=2.0)
        )
        first = await first_task
        assert first.success
        assert not conflict.success
        assert "idempotency conflict" in (conflict.error or "")
        assert len(device.write_calls) == 1
