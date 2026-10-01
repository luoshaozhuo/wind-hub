"""IEC104 remote-control command tracking.

Manages the lifecycle of pending control requests — activation,
confirmation, termination, and timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import CommandError

logger = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    """A remote-control request awaiting a response from the slave.

    Lifecycle::

        create ─► send ACT ─► receive ACT_CON ─► receive ACT_TERM ─► complete
                       │              │
                       └── timeout ◄──┴── negative confirmation
    """

    command: Command
    """The originating command."""

    ioa: int
    """Information object address."""

    future: asyncio.Future[CommandResult]
    """Resolved when the command completes (success, failure, or timeout)."""

    created_at: float = field(default_factory=time.monotonic)
    """``time.monotonic()`` of when this pending was registered."""

    timeout: float = 30.0
    """Maximum wait time in seconds for the full lifecycle."""

    activation_confirmed: bool = False
    """``True`` once ACT_CON (COT=7, P/N=0) has been received."""

    negative_confirmation: bool = False
    """``True`` if ACT_CON arrived with P/N=1 (negative)."""

    def _build_result(self, success: bool, error: str | None = None) -> CommandResult:
        return CommandResult(
            command_id=self.command.command_id,
            success=success,
            error=error,
        )


class PendingCommandRegistry:
    """Tracks in-flight remote-control requests.

    Enforces the constraint that at most one request targets a given
    IOA at any time.
    """

    def __init__(self) -> None:
        self._pending: dict[int, PendingCommand] = {}

    # ==================================================================
    # registration
    # ==================================================================

    def register(self, pending: PendingCommand) -> None:
        """Register *pending* and fail if the IOA is already in use.

        Raises:
            CommandError: If *ioa* already has an active pending command.
        """
        if pending.ioa in self._pending:
            existing = self._pending[pending.ioa]
            raise CommandError(
                f"IOA {pending.ioa} already has a pending command "
                f"'{existing.command.command_id}'",
                command_id=pending.command.command_id,
            )
        self._pending[pending.ioa] = pending
        logger.debug(
            "IEC104: registered pending command '%s' for IOA %d",
            pending.command.command_id,
            pending.ioa,
        )

    # ==================================================================
    # lifecycle callbacks
    # ==================================================================

    def on_activation_con(
        self,
        ioa: int,
        negative: bool,
    ) -> PendingCommand | None:
        """Handle an ACT_CON response.

        Args:
            ioa: Information object address from the response.
            negative: ``True`` if the P/N bit indicates a negative
                confirmation.

        Returns:
            The matching ``PendingCommand``, or ``None`` if no
            pending command targets *ioa*.
        """
        pending = self._pending.get(ioa)
        if pending is None:
            logger.debug(
                "IEC104: ACT_CON for IOA %d — no matching pending command",
                ioa,
            )
            return None

        pending.negative_confirmation = negative
        if negative:
            logger.warning(
                "IEC104: negative confirmation for IOA %d (cmd '%s')",
                ioa,
                pending.command.command_id,
            )
            # Resolve the future immediately on negative confirmation.
            if not pending.future.done():
                pending.future.set_result(
                    pending._build_result(
                        success=False,
                        error="negative confirmation",
                    )
                )
        else:
            pending.activation_confirmed = True
            logger.debug(
                "IEC104: activation confirmed for IOA %d (cmd '%s')",
                ioa,
                pending.command.command_id,
            )

        return pending

    def on_activation_term(self, ioa: int) -> PendingCommand | None:
        """Handle an ACT_TERM response.

        Removes the pending command from the registry and resolves the
        future as successful (provided it hasn't already been resolved
        by a negative confirmation).

        Returns:
            The removed ``PendingCommand``, or ``None``.
        """
        pending = self._pending.pop(ioa, None)
        if pending is None:
            logger.debug(
                "IEC104: ACT_TERM for IOA %d — no matching pending command",
                ioa,
            )
            return None

        if not pending.future.done():
            pending.future.set_result(pending._build_result(success=True))
        logger.debug(
            "IEC104: activation terminated for IOA %d (cmd '%s')",
            ioa,
            pending.command.command_id,
        )

        return pending

    # ==================================================================
    # cleanup
    # ==================================================================

    def remove(self, ioa: int) -> None:
        """Remove a pending command (e.g. on timeout or error)."""
        self._pending.pop(ioa, None)

    def get(self, ioa: int) -> PendingCommand | None:
        """Return the pending command for *ioa*, or ``None``."""
        return self._pending.get(ioa)

    def has_pending(self, ioa: int) -> bool:
        """``True`` if *ioa* has an active pending command."""
        return ioa in self._pending
