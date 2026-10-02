"""Unit tests for PendingCommand and PendingCommandRegistry."""

from __future__ import annotations

import asyncio

import pytest

from wind_hub.adapter.outbound.protocol.iec104.commands import (
    PendingCommand,
    PendingCommandRegistry,
)
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import CommandError

# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _make_cmd(command_id: str = "cmd-1", point_id: str = "p1") -> Command:
    return Command(
        command_id=command_id,
        device_id="d1",
        point_id=point_id,
        value=True,
    )


async def _make_pending(
    ioa: int = 100,
    command_id: str = "cmd-1",
    timeout: float = 30.0,
) -> PendingCommand:
    return PendingCommand(
        command=_make_cmd(command_id),
        ioa=ioa,
        future=asyncio.get_running_loop().create_future(),
        timeout=timeout,
    )


# ===========================================================================
# PendingCommand
# ===========================================================================


class TestPendingCommand:
    async def test_initial_state(self) -> None:
        """New PendingCommand starts with activation not confirmed."""
        cmd = _make_cmd()
        future = asyncio.get_running_loop().create_future()
        pc = PendingCommand(command=cmd, ioa=100, future=future)
        assert pc.command is cmd
        assert pc.ioa == 100
        assert pc.future is future
        assert pc.activation_confirmed is False
        assert pc.negative_confirmation is False
        assert pc.timeout == 30.0

    async def test_build_result_success(self) -> None:
        pc = await _make_pending()
        result = pc._build_result(success=True)
        assert result.command_id == "cmd-1"
        assert result.success is True
        assert result.error is None

    async def test_build_result_failure(self) -> None:
        pc = await _make_pending()
        result = pc._build_result(success=False, error="timeout")
        assert result.command_id == "cmd-1"
        assert result.success is False
        assert result.error == "timeout"

    async def test_custom_timeout(self) -> None:
        pc = await _make_pending(timeout=15.0)
        assert pc.timeout == 15.0


# ===========================================================================
# PendingCommandRegistry — registration
# ===========================================================================


class TestRegistryRegister:
    async def test_register_single(self) -> None:
        """A command can be registered for an unused IOA."""
        reg = PendingCommandRegistry()
        pc = await _make_pending()
        reg.register(pc)
        assert reg.has_pending(100) is True
        assert reg.get(100) is pc

    async def test_register_duplicate_raises(self) -> None:
        """Registering a second command for the same IOA raises CommandError."""
        reg = PendingCommandRegistry()
        pc1 = await _make_pending(command_id="cmd-1")
        reg.register(pc1)

        pc2 = await _make_pending(command_id="cmd-2")
        with pytest.raises(CommandError, match="IOA 100"):
            reg.register(pc2)

    async def test_multiple_different_ioas(self) -> None:
        """Commands for different IOAs can coexist."""
        reg = PendingCommandRegistry()
        pc1 = await _make_pending(ioa=100, command_id="c1")
        pc2 = await _make_pending(ioa=200, command_id="c2")
        reg.register(pc1)
        reg.register(pc2)
        assert reg.has_pending(100) is True
        assert reg.has_pending(200) is True


# ===========================================================================
# PendingCommandRegistry — lifecycle
# ===========================================================================


class TestRegistryActivationCon:
    async def test_positive_confirmation(self) -> None:
        """Positive ACT_CON sets activation_confirmed but does NOT resolve."""
        reg = PendingCommandRegistry()
        pc = await _make_pending()
        reg.register(pc)

        result = reg.on_activation_con(100, negative=False)
        assert result is pc
        assert pc.activation_confirmed is True
        assert pc.negative_confirmation is False
        assert not pc.future.done()  # Future not yet resolved

    async def test_negative_confirmation_resolves_future(self) -> None:
        """Negative ACT_CON resolves the future with failure."""
        reg = PendingCommandRegistry()
        pc = await _make_pending()
        reg.register(pc)

        result = reg.on_activation_con(100, negative=True)
        assert result is pc
        assert pc.negative_confirmation is True
        assert pc.future.done()
        r = pc.future.result()
        assert r.success is False
        assert r.error == "negative confirmation"

    def test_non_existent_ioa_returns_none(self) -> None:
        """ACT_CON for an IOA with no pending command returns None."""
        reg = PendingCommandRegistry()
        result = reg.on_activation_con(999, negative=False)
        assert result is None


class TestRegistryActivationTerm:
    async def test_activation_term_completes_command(self) -> None:
        """ACT_TERM removes the pending command and resolves future as success."""
        reg = PendingCommandRegistry()
        pc = await _make_pending()
        reg.register(pc)
        reg.on_activation_con(100, negative=False)

        result = reg.on_activation_term(100)
        assert result is pc
        assert reg.has_pending(100) is False
        assert pc.future.done()
        assert pc.future.result().success is True

    async def test_activation_term_after_negative_does_not_resolve_again(self) -> None:
        """ACT_TERM after negative confirmation should not change the result."""
        reg = PendingCommandRegistry()
        pc = await _make_pending()
        reg.register(pc)

        reg.on_activation_con(100, negative=True)
        assert pc.future.done()

        result = reg.on_activation_term(100)
        assert result is pc
        assert reg.has_pending(100) is False
        assert pc.future.result().success is False  # Still negative.

    def test_non_existent_ioa_returns_none(self) -> None:
        """ACT_TERM for an IOA with no pending command returns None."""
        reg = PendingCommandRegistry()
        result = reg.on_activation_term(999)
        assert result is None


# ===========================================================================
# PendingCommandRegistry — remove / get / has_pending
# ===========================================================================


class TestRegistryAccessors:
    async def test_remove_existing(self) -> None:
        reg = PendingCommandRegistry()
        reg.register(await _make_pending())
        reg.remove(100)
        assert reg.has_pending(100) is False
        assert reg.get(100) is None

    def test_remove_non_existent_noop(self) -> None:
        """Removing a non-existent IOA should not raise."""
        reg = PendingCommandRegistry()
        reg.remove(999)  # Should not raise.

    async def test_get_existing(self) -> None:
        reg = PendingCommandRegistry()
        pc = await _make_pending()
        reg.register(pc)
        assert reg.get(100) is pc

    def test_get_non_existent(self) -> None:
        reg = PendingCommandRegistry()
        assert reg.get(999) is None

    async def test_has_pending_true(self) -> None:
        reg = PendingCommandRegistry()
        reg.register(await _make_pending())
        assert reg.has_pending(100) is True

    def test_has_pending_false(self) -> None:
        reg = PendingCommandRegistry()
        assert reg.has_pending(100) is False
