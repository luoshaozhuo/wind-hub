"""IEC 60870-5-104 flow control: 15-bit sequence numbers and k/w windows."""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_SEQ = 0x7FFF  # 32767 — 15-bit sequence space


def _seq_diff(later: int, earlier: int) -> int:
    """Count of sequence numbers from *earlier* up to (but not including) *later*.

    Handles 15-bit wraparound modulo 32768.
    """
    return (later - earlier) & MAX_SEQ


# ==========================================================================
# SequenceNumbers
# ==========================================================================


@dataclass
class SequenceNumbers:
    """I-frame 15-bit send/receive sequence counters with wraparound.

    Used by exactly one session — the send side increments ``send_seq``
    and the receive side increments ``recv_seq``.
    """

    _send_seq: int = field(default=0, init=False)
    _recv_seq: int = field(default=0, init=False)

    @property
    def send_seq(self) -> int:
        """Next send sequence number N(S) to use."""
        return self._send_seq

    @property
    def recv_seq(self) -> int:
        """Next expected receive sequence number N(R)."""
        return self._recv_seq

    def next_send(self) -> int:
        """Return the current send sequence number and advance it (mod 32768)."""
        current = self._send_seq
        self._send_seq = (current + 1) & MAX_SEQ
        return current

    def next_recv(self) -> int:
        """Return the current receive sequence number and advance it (mod 32768)."""
        current = self._recv_seq
        self._recv_seq = (current + 1) & MAX_SEQ
        return current

    def checkpoint(self) -> tuple[int, int]:
        """Return (send_seq, recv_seq) snapshot."""
        return (self._send_seq, self._recv_seq)

    def recv_seq_for_ack(self) -> int:
        """The N(R) value to put in outbound I/S-frames.

        This is the sequence number of the *next* I-frame we expect to
        receive — i.e. ``recv_seq`` itself.
        """
        return self._recv_seq


# ==========================================================================
# FlowController
# ==========================================================================


@dataclass
class FlowController:
    """Enforces IEC104 k/w flow-control windows.

    - **k** (send window): maximum number of unacknowledged I-frames we
      may send before waiting for an acknowledgement from the peer.
    - **w** (receive window): maximum number of I-frames we may receive
      before we **must** send an S-frame acknowledgement.
    """

    k: int = 12
    """Send window size — maximum unacked outbound I-frames."""

    w: int = 8
    """Receive window size — maximum inbound I-frames before S-frame ack."""

    _seq: SequenceNumbers = field(default_factory=SequenceNumbers, init=False)
    _acked_up_to: int = field(default=0, init=False)
    """The send sequence number the peer has acknowledged (N(R) from
    latest S-frame or I-frame)."""

    _unacked_recv_count: int = field(default=0, init=False)
    """Number of received I-frames we haven't acknowledged yet."""

    # ------------------------------------------------------------------
    # send flow control
    # ------------------------------------------------------------------

    @property
    def can_send(self) -> bool:
        """``True`` when the send window is not full (unacked < k)."""
        return _seq_diff(self._seq.send_seq, self._acked_up_to) < self.k

    @property
    def outstanding(self) -> int:
        """Number of sent but not-yet-acknowledged I-frames."""
        return _seq_diff(self._seq.send_seq, self._acked_up_to)

    def next_send_seq(self) -> int:
        """Return the next N(S) to use and advance the counter.

        Call this **before** building an I-frame so the sequence number
        is correct.
        """
        return self._seq.next_send()

    def on_sent(self) -> None:
        """Call after an I-frame has been sent — advances the sequence.

        .. deprecated::
            Use :meth:`next_send_seq` instead — it returns the sequence
            number atomically.
        """
        self._seq.next_send()

    def on_ack(self, recv_seq: int) -> None:
        """Process a peer acknowledgement — an N(R) in an S-frame or I-frame.

        All outbound I-frames with ``N(S) < recv_seq`` are now considered
        acknowledged.
        """
        self._acked_up_to = recv_seq & MAX_SEQ

    def ack_is_outstanding(self) -> bool:
        """``True`` when we have sent I-frames that haven't been acked."""
        return _seq_diff(self._seq.send_seq, self._acked_up_to) > 0

    # ------------------------------------------------------------------
    # receive flow control
    # ------------------------------------------------------------------

    @property
    def needs_ack(self) -> bool:
        """``True`` when we've received >= *w* unacknowledged I-frames
        and must send an S-frame soon."""
        return self._unacked_recv_count >= self.w

    def on_received(self) -> None:
        """Call after a valid I-frame has been received."""
        self._seq.next_recv()
        self._unacked_recv_count += 1

    def on_ack_sent(self) -> None:
        """Call after sending an S-frame to reset the receive-counter."""
        self._unacked_recv_count = 0

    def recv_seq_for_ack(self) -> int:
        """The N(R) value to put in an outbound S-frame or I-frame."""
        return self._seq.recv_seq_for_ack()
