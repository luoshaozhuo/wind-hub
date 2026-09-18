"""IEC 60870-5-104 connection state machine.

Tracks the lifecycle of a single TCP connection to an IEC104 slave.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import ClassVar


class ConnectionState(Enum):
    """IEC 60870-5-104 connection lifecycle states.

    ::

        DISCONNECTED ──► TCP_CONNECTED ──► STARTDT_PENDING ──► STARTED
             ▲                 ▲                │              │   │
             │                 │                ▼              │   │
             │                 │            STOPPED ◄──────────┘   │
             │                 │                │                   │
             │                 └────────────────┼───────────────────┘
             │                                  │
             └─── (from any state) ──► FAILED ◄─┘
    """

    DISCONNECTED = auto()
    """No TCP connection established; idle or reconnecting."""

    TCP_CONNECTED = auto()
    """TCP socket is open but the IEC104 STARTDT handshake hasn't
    been performed yet."""

    STARTDT_PENDING = auto()
    """STARTDT act has been sent; waiting for STARTDT con."""

    STARTED = auto()
    """Data transfer is active: I-frames can flow."""

    STOPPED = auto()
    """Data transfer has been halted (STOPDT sent / received)."""

    FAILED = auto()
    """Connection has reached an unrecoverable error.  External
    intervention (e.g. manual re-connect) is required to leave
    this state."""


class ConnectionStateMachine:
    """Legal-transition–enforcing state machine for IEC104 connections.

    *Any* state can transition to ``FAILED``.  Only ``FAILED`` can
    transition to ``DISCONNECTED`` (manual reset).
    """

    # Maps current state → set of allowed next states.
    _TRANSITIONS: ClassVar[dict[ConnectionState, set[ConnectionState]]] = {
        ConnectionState.DISCONNECTED: {
            ConnectionState.TCP_CONNECTED,
            ConnectionState.FAILED,
        },
        ConnectionState.TCP_CONNECTED: {
            ConnectionState.STARTDT_PENDING,
            ConnectionState.DISCONNECTED,
            ConnectionState.FAILED,
        },
        ConnectionState.STARTDT_PENDING: {
            ConnectionState.STARTED,
            ConnectionState.DISCONNECTED,
            ConnectionState.FAILED,
        },
        ConnectionState.STARTED: {
            ConnectionState.STOPPED,
            ConnectionState.DISCONNECTED,
            ConnectionState.FAILED,
        },
        ConnectionState.STOPPED: {
            ConnectionState.STARTDT_PENDING,
            ConnectionState.DISCONNECTED,
            ConnectionState.FAILED,
        },
        ConnectionState.FAILED: {
            ConnectionState.DISCONNECTED,
        },
    }

    def __init__(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the state implies an open TCP connection."""
        return self._state in {
            ConnectionState.TCP_CONNECTED,
            ConnectionState.STARTDT_PENDING,
            ConnectionState.STARTED,
            ConnectionState.STOPPED,
        }

    @property
    def is_started(self) -> bool:
        """Return ``True`` when data transfer is active."""
        return self._state == ConnectionState.STARTED

    # ------------------------------------------------------------------
    # transitions
    # ------------------------------------------------------------------

    def transition(self, new_state: ConnectionState) -> None:
        """Move to *new_state*, raising ValueError on illegal transitions.

        Raises:
            ValueError: If the transition is not allowed.
        """
        allowed = self._TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise ValueError(f"Illegal state transition: {self._state.name} → " f"{new_state.name}")
        self._state = new_state

    def to_disconnected(self) -> None:
        """Transition to DISCONNECTED."""
        self.transition(ConnectionState.DISCONNECTED)

    def to_tcp_connected(self) -> None:
        """Transition to TCP_CONNECTED."""
        self.transition(ConnectionState.TCP_CONNECTED)

    def to_startdt_pending(self) -> None:
        """Transition to STARTDT_PENDING."""
        self.transition(ConnectionState.STARTDT_PENDING)

    def to_started(self) -> None:
        """Transition to STARTED."""
        self.transition(ConnectionState.STARTED)

    def to_stopped(self) -> None:
        """Transition to STOPPED."""
        self.transition(ConnectionState.STOPPED)

    def to_failed(self) -> None:
        """Transition to FAILED."""
        self._state = ConnectionState.FAILED  # always allowed

    # ------------------------------------------------------------------
    # convenience checks
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"ConnectionStateMachine({self._state.name})"
