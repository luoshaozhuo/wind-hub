"""Unit tests for Dispatcher — command routing, idempotency, timeout."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from wind_hub.domain.command.dispatcher import Dispatcher
from wind_hub.domain.model.command import Command, CommandResult

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
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmd = _make_cmd()

    result = await dispatcher.send(cmd)

    assert result.command_id == "cmd-001"
    assert result.success is True
    assert result.error is None


# ---------------------------------------------------------------------------
# send — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_idempotent_returns_cached_result() -> None:
    """Same command_id returns cached result on second call."""
    proto = _make_proto()
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmd = _make_cmd()

    result1 = await dispatcher.send(cmd)
    result2 = await dispatcher.send(cmd)

    assert result1.command_id == result2.command_id
    assert result1.success == result2.success
    # second call should NOT hit the protocol (cache hit)
    assert dispatcher.cache_size == 1


# ---------------------------------------------------------------------------
# send — unknown device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_unknown_device_returns_failure() -> None:
    """send fails when the target device is not in the protocols dict."""
    dispatcher = Dispatcher(protocols={})
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
    ProtocolPort.write exceeds the command timeout."""
    proto = _make_proto(delay=10.0)
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmd = _make_cmd(timeout=0.05)  # very short timeout

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert result.error == "write timeout after 0.1s"


@pytest.mark.asyncio
async def test_send_timeout_uses_default_when_command_has_none() -> None:
    """Command.timeout <= 0 时回退到注入的默认写超时（system.yaml
    scheduler.write_timeout），并同样返回 write 阶段定位的错误。"""
    proto = _make_proto(delay=10.0)
    dispatcher = Dispatcher(protocols={"dev-1": proto}, default_timeout=0.05)
    cmd = _make_cmd(timeout=0.0)  # 未自带超时 → 用默认值

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert result.error == "write timeout after 0.1s"


@pytest.mark.asyncio
async def test_send_command_timeout_overrides_smaller_default() -> None:
    """Command.timeout > 0 时优先于默认写超时——默认 0.05s 会超时，
    但命令自带 1s 超时让 0.1s 的写成功。"""
    proto = _make_proto(delay=0.1)
    dispatcher = Dispatcher(protocols={"dev-1": proto}, default_timeout=0.05)
    cmd = _make_cmd(timeout=1.0)

    result = await dispatcher.send(cmd)

    assert result.success is True


# ---------------------------------------------------------------------------
# send — protocol error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_protocol_error_returns_failure() -> None:
    """send catches exceptions from ProtocolPort.write and returns them
    as CommandResult(success=False)."""
    proto = _make_proto(succeed=False)
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmd = _make_cmd()

    result = await dispatcher.send(cmd)

    assert result.success is False
    assert "simulated protocol error" in (result.error or "")


# ---------------------------------------------------------------------------
# send_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_batch_concurrent() -> None:
    """send_batch executes multiple commands concurrently."""
    proto = _make_proto()
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmds = [_make_cmd(command_id=f"cmd-{i:03d}") for i in range(5)]

    results = await dispatcher.send_batch(cmds)

    assert len(results) == 5
    for r in results:
        assert r.success is True
        assert r.error is None


@pytest.mark.asyncio
async def test_send_batch_preserves_order() -> None:
    """send_batch result order matches input order."""
    proto = _make_proto()
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmds = [_make_cmd(command_id=f"cmd-{i:03d}") for i in range(3)]

    results = await dispatcher.send_batch(cmds)

    assert [r.command_id for r in results] == [c.command_id for c in cmds]


@pytest.mark.asyncio
async def test_send_batch_with_exception() -> None:
    """send_batch handles one command raising an exception without
    affecting other commands."""
    proto_bad = _make_proto(succeed=False)  # raises on write
    proto_good = _make_proto()
    dispatcher = Dispatcher(protocols={"dev-bad": proto_bad, "dev-good": proto_good})
    cmds = [
        _make_cmd(command_id="bad", device_id="dev-bad"),
        _make_cmd(command_id="good", device_id="dev-good"),
    ]

    results = await dispatcher.send_batch(cmds)

    assert results[0].success is False
    assert results[1].success is True


# ---------------------------------------------------------------------------
# cache management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_cache_resets_idempotency() -> None:
    """clear_cache removes all cached results so commands re-execute."""
    proto = _make_proto()
    dispatcher = Dispatcher(protocols={"dev-1": proto})
    cmd = _make_cmd()

    await dispatcher.send(cmd)
    assert dispatcher.cache_size == 1

    dispatcher.clear_cache()
    assert dispatcher.cache_size == 0

    r2 = await dispatcher.send(cmd)
    # Both should succeed (re-executed, not cached)
    assert r2.success is True


def test_cache_size_property() -> None:
    """cache_size reflects the number of cached entries."""
    dispatcher = Dispatcher(protocols={})
    assert dispatcher.cache_size == 0


# ---------------------------------------------------------------------------
# cache TTL expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_ttl_expiry_re_executes() -> None:
    """After TTL expires, the same command_id is re-executed."""
    proto = _make_proto()
    # Very short TTL
    dispatcher = Dispatcher(
        protocols={"dev-1": proto},
        idempotency_ttl=0.01,
    )
    cmd = _make_cmd()

    await dispatcher.send(cmd)
    assert dispatcher.cache_size == 1

    # Wait past TTL
    await asyncio.sleep(0.02)

    r2 = await dispatcher.send(cmd)
    # Should re-execute (not return stale cache)
    assert r2.success is True


# ---------------------------------------------------------------------------
# 成功/失败计数回调（决策 6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_success_fires_sent_callback() -> None:
    sent: list[str] = []
    failed: list[str] = []
    dispatcher = Dispatcher(
        protocols={"dev-1": _make_proto()},
        on_command_sent=lambda: sent.append("s"),
        on_command_failed=lambda: failed.append("f"),
    )
    result = await dispatcher.send(_make_cmd())
    assert result.success is True
    assert sent == ["s"]
    assert failed == []


@pytest.mark.asyncio
async def test_send_failure_fires_failed_callback() -> None:
    sent: list[str] = []
    failed: list[str] = []
    dispatcher = Dispatcher(
        protocols={"dev-1": _make_proto(succeed=False)},
        on_command_sent=lambda: sent.append("s"),
        on_command_failed=lambda: failed.append("f"),
    )
    result = await dispatcher.send(_make_cmd())
    assert result.success is False
    assert sent == []
    assert failed == ["f"]


@pytest.mark.asyncio
async def test_send_unknown_device_counts_as_failed() -> None:
    failed: list[str] = []
    dispatcher = Dispatcher(protocols={}, on_command_failed=lambda: failed.append("f"))
    result = await dispatcher.send(_make_cmd(device_id="ghost"))
    assert result.success is False
    assert failed == ["f"]


@pytest.mark.asyncio
async def test_send_timeout_counts_as_failed() -> None:
    failed: list[str] = []
    dispatcher = Dispatcher(
        protocols={"dev-1": _make_proto(delay=10.0)},
        on_command_failed=lambda: failed.append("f"),
    )
    result = await dispatcher.send(_make_cmd(timeout=0.05))
    assert result.success is False
    assert result.error is not None and result.error.startswith("write timeout")
    assert failed == ["f"]


@pytest.mark.asyncio
async def test_cache_hit_does_not_double_count() -> None:
    sent: list[str] = []
    dispatcher = Dispatcher(
        protocols={"dev-1": _make_proto()},
        on_command_sent=lambda: sent.append("s"),
    )
    cmd = _make_cmd()
    await dispatcher.send(cmd)
    await dispatcher.send(cmd)  # 幂等缓存命中——首次执行已计过，不得重复累计
    assert sent == ["s"]
