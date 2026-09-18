"""Unit tests for IEC104 flow control — SequenceNumbers and FlowController."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.iec104.flow import (
    MAX_SEQ,
    FlowController,
    SequenceNumbers,
    _seq_diff,
)


class TestSeqDiff:
    """Tests for the _seq_diff helper."""

    def test_simple_range(self) -> None:
        assert _seq_diff(5, 0) == 5

    def test_zero_diff(self) -> None:
        assert _seq_diff(10, 10) == 0

    def test_wraparound(self) -> None:
        # later=5 means we've wrapped from 32767→0…→5, so 5 is
        # actually later than 32760.
        diff = _seq_diff(5, 32760)
        assert diff == (5 - 32760) & MAX_SEQ
        assert diff > 0  # must be positive after wraparound

    def test_boundary_wraparound(self) -> None:
        # later=0, earlier=32767 → diff = 1
        assert _seq_diff(0, 32767) == 1


class TestSequenceNumbers:
    """Tests for the SequenceNumbers class."""

    def test_initial_values(self) -> None:
        seq = SequenceNumbers()
        assert seq.send_seq == 0
        assert seq.recv_seq == 0

    def test_next_send(self) -> None:
        seq = SequenceNumbers()
        assert seq.next_send() == 0
        assert seq.send_seq == 1
        assert seq.next_send() == 1
        assert seq.send_seq == 2

    def test_next_recv(self) -> None:
        seq = SequenceNumbers()
        assert seq.next_recv() == 0
        assert seq.recv_seq == 1

    def test_send_wraparound(self) -> None:
        seq = SequenceNumbers()
        # Advance to MAX_SEQ.
        seq._send_seq = MAX_SEQ
        assert seq.next_send() == MAX_SEQ  # returns 32767
        assert seq.send_seq == 0  # wraps to 0

    def test_recv_wraparound(self) -> None:
        seq = SequenceNumbers()
        seq._recv_seq = MAX_SEQ
        assert seq.next_recv() == MAX_SEQ  # returns 32767
        assert seq.recv_seq == 0  # wraps to 0

    def test_checkpoint(self) -> None:
        seq = SequenceNumbers()
        seq.next_send()
        seq.next_send()
        seq.next_recv()
        assert seq.checkpoint() == (2, 1)

    def test_recv_seq_for_ack(self) -> None:
        seq = SequenceNumbers()
        assert seq.recv_seq_for_ack() == 0
        seq.next_recv()
        assert seq.recv_seq_for_ack() == 1


class TestFlowController:
    """Tests for the FlowController with k=4, w=3 (smaller windows)."""

    @pytest.fixture
    def fc(self) -> FlowController:
        return FlowController(k=4, w=3)

    # ------------------------------------------------------------------
    # send flow control
    # ------------------------------------------------------------------

    def test_can_send_initially(self, fc: FlowController) -> None:
        assert fc.can_send is True
        assert fc.outstanding == 0

    def test_can_send_after_k_frames(self, fc: FlowController) -> None:
        for _ in range(4):  # k=4, after 4 sends window is full
            fc.on_sent()
        assert fc.can_send is False
        assert fc.outstanding == 4

    def test_can_send_after_ack(self, fc: FlowController) -> None:
        for _ in range(4):
            fc.on_sent()
        assert fc.can_send is False

        # Acknowledge all 4 sent frames.
        fc.on_ack(4)
        assert fc.can_send is True
        assert fc.outstanding == 0

    def test_partial_ack(self, fc: FlowController) -> None:
        for _ in range(4):
            fc.on_sent()
        # Acknowledge first 2.
        fc.on_ack(2)
        assert fc.outstanding == 2
        assert fc.can_send is True

    def test_ack_is_outstanding(self, fc: FlowController) -> None:
        assert fc.ack_is_outstanding() is False
        fc.on_sent()
        assert fc.ack_is_outstanding() is True
        fc.on_ack(1)
        assert fc.ack_is_outstanding() is False

    def test_on_ack_masks_recv_seq(self, fc: FlowController) -> None:
        """on_ack should mask recv_seq to 15 bits."""
        for _ in range(4):
            fc.on_sent()
        # Pass recv_seq with bits above 15 set.
        fc.on_ack(4 | 0x8000)
        assert fc.outstanding == 0
        assert fc.can_send is True

    # ------------------------------------------------------------------
    # receive flow control
    # ------------------------------------------------------------------

    def test_needs_ack_initially(self, fc: FlowController) -> None:
        assert fc.needs_ack is False

    def test_needs_ack_after_w_frames(self, fc: FlowController) -> None:
        for _ in range(3):  # w=3
            fc.on_received()
        assert fc.needs_ack is True

    def test_needs_ack_reset_after_ack_sent(self, fc: FlowController) -> None:
        for _ in range(3):
            fc.on_received()
        assert fc.needs_ack is True
        fc.on_ack_sent()
        assert fc.needs_ack is False

    def test_recv_seq_for_ack(self, fc: FlowController) -> None:
        assert fc.recv_seq_for_ack() == 0
        fc.on_received()
        assert fc.recv_seq_for_ack() == 1
        fc.on_received()
        assert fc.recv_seq_for_ack() == 2

    # ------------------------------------------------------------------
    # mixed operation
    # ------------------------------------------------------------------

    def test_full_cycle(self, fc: FlowController) -> None:
        """Simulate a typical send/receive cycle."""
        # Receive 2 I-frames.
        fc.on_received()
        fc.on_received()
        assert fc.needs_ack is False  # w=3, so not yet
        assert fc.recv_seq_for_ack() == 2

        # Send 3 I-frames.
        for _ in range(3):
            fc.on_sent()
        assert fc.outstanding == 3
        assert fc.can_send is True  # k=4, still room

        # Receive S-frame ack for first 2.
        fc.on_ack(2)
        assert fc.outstanding == 1

        # Receive 3rd I-frame → needs_ack.
        fc.on_received()
        assert fc.needs_ack is True

        # Send S-frame ack.
        fc.on_ack_sent()
        assert fc.needs_ack is False
