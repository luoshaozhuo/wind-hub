"""Unit tests for CommandDispatcher — command dispatch, idempotency, timeout."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from wind_hub_collector.application.command_dispatcher import CommandDispatcher
from wind_hub_collector.application.runtime.device import Device
from wind_hub_core.config.schema import DeviceConfig, Endpoint
from wind_hub_core.model.command import Command, CommandResult

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_proto(succeed: bool = True, delay: float = 0.0) -> AsyncMock:
    """Create a mock ProtocolPort with optional delay."""

    async def _write(cmds: list[Command]) -> list[CommandResult]:
        if delay:
            await asyncio.sleep(delay)
        if not succeed:
            raise RuntimeError("simulated protocol error")
        return [CommandResult(command_id=c.command_id, success=True) for c in cmds]

    proto = AsyncMock()
    proto.write = _write
    return proto


def _make_device(device_id: str, proto: AsyncMock) -> Device:
    """Wrap a mock protocol in a runtime Device."""
    config = DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        endpoint=Endpoint(host="127.0.0.1", port=502),
        point_table="t1",
    )
    return Device(config, [], proto)


def _make_devices(mapping: dict[str, AsyncMock]) -> dict[str, Device]:
    return {dev_id: _make_device(dev_id, proto) for dev_id, proto in mapping.items()}


def _make_cmd(
    command_id: str = "cmd-001",
    device_id: str = "dev-1",
    point_id: str = "pt-1",
    value: object = 42.0,
    timeout: float = 5.0,
) -> Command:
    return Command(
        command_id=command_id,
        device_id=device_id,
        point_id=point_id,
        value=value,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# send — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_success() -> None:
    """send returns CommandResult(success=True) on normal completion."""
    proto = _make_proto()
    dispatcher = CommandDispatcher(devices=_make_devices({"dev-1": proto}))
    cmd = _make_cmd()

    result = await dispatcher.send(cmd)

    assert result.command_id == "cmd-001"
    assert result.success is True
    assert result.error is None


@pytest.mark.asyncio
async def test_send_delegates_to_device_write() -> None:
    """CommandDispatcher 经 ``Device.write`` 下发——不直接触碰 ProtocolPort。"""
    written: list[list[Command]] = []

    async def _write(cmds: list[Command]) -> list[CommandResult]:
        written.append(cmds)
        return [CommandResult(command_id=c.command_id, success=True) for c in cmds]

    device = _make_device("dev-1", _make_proto())
    device._protocol.write = _write  # noqa: SLF001 —— 验证经 Device.write 委托到协议
    dispatcher = CommandDispatcher(devices={"dev-1": device})
    cmd = _make_cmd()

    result = await dispatcher.send(cmd)

    assert result.success is True
    assert written == [[cmd]]


# ---------------------------------------------------------------------------
# send — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_idempotent_returns_cached_result() -> None:
    """Same command_id returns cached result on second call."""
    proto = _make_proto()
    dispatcher = CommandDispatcher(devices=_make_devices({"dev-1": proto}))
    cmd = _make_cmd()

    result1 = await dispatcher.send(cmd)
    result2 = await dispatcher.send(cmd)

    assert result1.command_id == result2.command_id
    assert result1.success == result2.success


@pytest.mark.asyncio
async def test_concurrent_same_command_id_writes_device_once() -> None:
    """并发相同 command_id 只能执行一次真实设备写入。"""
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _write(cmds: list[Command]) -> list[CommandResult]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return [CommandResult(command_id=cmds[0].command_id, success=True)]

    device = _make_device("dev-1", _make_proto())
    device._protocol.write = _write  # noqa: SLF001
    dispatcher = CommandDispatcher(devices={"dev-1": device})
    cmd = _make_cmd()

    requests = [asyncio.create_task(dispatcher.send(cmd)) for _ in range(100)]
    await entered.wait()
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    results = await asyncio.gather(*requests)

    assert calls == 1
    assert all(result == results[0] for result in results)
    assert results[0].success is True


@pytest.mark.asyncio
async def test_concurrent_same_command_id_shares_failure_result() -> None:
    """并发相同 command_id 的协议失败只执行一次并共享失败结果。"""
    calls = 0

    async def _write(cmds: list[Command]) -> list[CommandResult]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        raise RuntimeError("simulated protocol error")

    device = _make_device("dev-1", _make_proto())
    device._protocol.write = _write  # noqa: SLF001
    dispatcher = CommandDispatcher(devices={"dev-1": device})
    cmd = _make_cmd()

    results = await asyncio.gather(*(dispatcher.send(cmd) for _ in range(20)))

    assert calls == 1
    assert all(result == results[0] for result in results)
    assert results[0].success is False
    assert "simulated protocol error" in (results[0].error or "")


@pytest.mark.asyncio
async def test_concurrent_same_command_id_shares_timeout_result() -> None:
    """并发相同 command_id 的写超时只产生一次真实执行。"""
    calls = 0

    async def _write(cmds: list[Command]) -> list[CommandResult]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.0)
        return [CommandResult(command_id=cmds[0].command_id, success=True)]

    device = _make_device("dev-1", _make_proto())
    device._protocol.write = _write  # noqa: SLF001
    dispatcher = CommandDispatcher(devices={"dev-1": device})
    cmd = _make_cmd(timeout=0.02)

    results = await asyncio.gather(*(dispatcher.send(cmd) for _ in range(20)))

    assert calls == 1
    assert all(result == results[0] for result in results)
    assert results[0].success is False
    assert results[0].error is not None
    assert results[0].error.startswith("write timeout")


# ---------------------------------------------------------------------------
# send — unknown device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_unknown_device_returns_failure() -> None:
    """send fails when the target device is not in the devices dict."""
    dispatcher = CommandDispatcher(devices={})
    cmd = _make_cmd(device_id="nonexistent")

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert "nonexistent" in (result.error or "")


# ---------------------------------------------------------------------------
# send — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_timeout_returns_failure() -> None:
    """send returns failure with a stage-located 'write timeout' error when
    Device.write exceeds the command timeout."""
    proto = _make_proto(delay=10.0)
    dispatcher = CommandDispatcher(devices=_make_devices({"dev-1": proto}))
    cmd = _make_cmd(timeout=0.05)  # very short timeout

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert result.error == "write timeout after 0.1s"


@pytest.mark.asyncio
async def test_send_timeout_uses_default_when_command_has_none() -> None:
    """Command.timeout <= 0 时回退到注入的默认写超时（system.yaml
    runtime.write_timeout），并同样返回 write 阶段定位的错误。"""
    proto = _make_proto(delay=10.0)
    dispatcher = CommandDispatcher(devices=_make_devices({"dev-1": proto}), default_timeout=0.05)
    cmd = _make_cmd(timeout=0.0)  # 未自带超时 → 用默认值

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert result.error == "write timeout after 0.1s"


@pytest.mark.asyncio
async def test_send_command_timeout_overrides_smaller_default() -> None:
    """Command.timeout > 0 时优先于默认写超时——默认 0.05s 会超时，
    但命令自带 1s 超时让 0.1s 的写成功。"""
    proto = _make_proto(delay=0.1)
    dispatcher = CommandDispatcher(devices=_make_devices({"dev-1": proto}), default_timeout=0.05)
    cmd = _make_cmd(timeout=1.0)

    result = await dispatcher.send(cmd)

    assert result.success is True


# ---------------------------------------------------------------------------
# send — protocol error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_protocol_error_returns_failure() -> None:
    """send catches exceptions from Device.write and returns them
    as CommandResult(success=False)."""
    proto = _make_proto(succeed=False)
    dispatcher = CommandDispatcher(devices=_make_devices({"dev-1": proto}))
    cmd = _make_cmd()

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert "simulated protocol error" in (result.error or "")


# ---------------------------------------------------------------------------
# cache TTL expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_ttl_expiry_re_executes() -> None:
    """After TTL expires, the same command_id is re-executed."""
    proto = _make_proto()
    # Very short TTL
    dispatcher = CommandDispatcher(
        devices=_make_devices({"dev-1": proto}),
        idempotency_ttl=0.01,
    )
    cmd = _make_cmd()

    await dispatcher.send(cmd)
    # Wait past TTL
    await asyncio.sleep(0.02)

    r2 = await dispatcher.send(cmd)
    # Should re-execute (not return stale cache)
    assert r2.success is True


