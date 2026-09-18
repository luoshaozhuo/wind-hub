"""Unit tests for IEC104 connection state machine."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.iec104.connection import (
    ConnectionState,
    ConnectionStateMachine,
)


class TestConnectionState:
    """Test the ConnectionState enum."""

    def test_all_states_exist(self) -> None:
        """Verify all expected states are defined."""
        names = {s.name for s in ConnectionState}
        expected = {
            "DISCONNECTED",
            "TCP_CONNECTED",
            "STARTDT_PENDING",
            "STARTED",
            "STOPPED",
            "FAILED",
        }
        assert names == expected

    def test_state_values_are_unique(self) -> None:
        """Each state has a unique auto-value."""
        values = [s.value for s in ConnectionState]
        assert len(values) == len(set(values))


class TestConnectionStateMachine:
    """Test the ConnectionStateMachine."""

    def test_initial_state_is_disconnected(self) -> None:
        sm = ConnectionStateMachine()
        assert sm.state == ConnectionState.DISCONNECTED

    # ----------------------------------------------------------------
    # legal transitions
    # ----------------------------------------------------------------

    def test_disconnected_to_tcp_connected(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        assert sm.state == ConnectionState.TCP_CONNECTED

    def test_tcp_connected_to_startdt_pending(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_startdt_pending()
        assert sm.state == ConnectionState.STARTDT_PENDING

    def test_startdt_pending_to_started(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_startdt_pending()
        sm.to_started()
        assert sm.state == ConnectionState.STARTED

    def test_started_to_stopped(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_startdt_pending()
        sm.to_started()
        sm.to_stopped()
        assert sm.state == ConnectionState.STOPPED

    def test_stopped_to_startdt_pending(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_startdt_pending()
        sm.to_started()
        sm.to_stopped()
        sm.to_startdt_pending()
        assert sm.state == ConnectionState.STARTDT_PENDING

    def test_any_to_failed(self) -> None:
        """Any state can transition to FAILED."""
        sm = ConnectionStateMachine()
        sm.to_failed()
        assert sm.state == ConnectionState.FAILED

    def test_failed_to_disconnected(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_failed()
        sm.to_disconnected()
        assert sm.state == ConnectionState.DISCONNECTED

    def test_tcp_connected_to_disconnected(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_disconnected()
        assert sm.state == ConnectionState.DISCONNECTED

    def test_started_to_disconnected(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_startdt_pending()
        sm.to_started()
        sm.to_disconnected()
        assert sm.state == ConnectionState.DISCONNECTED

    # ----------------------------------------------------------------
    # illegal transitions
    # ----------------------------------------------------------------

    @pytest.mark.parametrize(
        "illegal_target",
        [
            ConnectionState.STARTDT_PENDING,  # can't go directly from DISCONNECTED
            ConnectionState.STARTED,  # need TCP_CONNECTED + STARTDT_PENDING
            ConnectionState.STOPPED,  # not from DISCONNECTED
        ],
    )
    def test_disconnected_illegal_transitions(
        self,
        illegal_target: ConnectionState,
    ) -> None:
        sm = ConnectionStateMachine()
        with pytest.raises(ValueError, match="Illegal state transition"):
            sm.transition(illegal_target)

    def test_failed_only_goes_to_disconnected(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_failed()
        for target in ConnectionState:
            if target == ConnectionState.DISCONNECTED:
                continue
            with pytest.raises(ValueError, match="Illegal state transition"):
                sm.transition(target)

    def test_started_cannot_go_to_tcp_connected(self) -> None:
        sm = ConnectionStateMachine()
        sm.to_tcp_connected()
        sm.to_startdt_pending()
        sm.to_started()
        with pytest.raises(ValueError, match="Illegal state transition"):
            sm.to_tcp_connected()

    # ----------------------------------------------------------------
    # convenience properties
    # ----------------------------------------------------------------

    def test_is_connected_property(self) -> None:
        sm = ConnectionStateMachine()
        assert not sm.is_connected  # DISCONNECTED

        sm.to_tcp_connected()
        assert sm.is_connected

        sm.to_startdt_pending()
        assert sm.is_connected

        sm.to_started()
        assert sm.is_connected

        sm.to_stopped()
        assert sm.is_connected

        sm.to_disconnected()
        assert not sm.is_connected

        sm.to_failed()
        assert not sm.is_connected

    def test_is_started_property(self) -> None:
        sm = ConnectionStateMachine()
        assert not sm.is_started

        sm.to_tcp_connected()
        assert not sm.is_started

        sm.to_startdt_pending()
        assert not sm.is_started

        sm.to_started()
        assert sm.is_started

        sm.to_stopped()
        assert not sm.is_started

    # ----------------------------------------------------------------
    # repr
    # ----------------------------------------------------------------

    def test_repr(self) -> None:
        sm = ConnectionStateMachine()
        assert "DISCONNECTED" in repr(sm)
        sm.to_tcp_connected()
        assert "TCP_CONNECTED" in repr(sm)
