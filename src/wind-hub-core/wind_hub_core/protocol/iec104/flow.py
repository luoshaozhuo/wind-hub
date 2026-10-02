"""IEC104 15-bit 序号与 k/w 窗口流控。"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_SEQ = 0x7FFF  # 15-bit 序号空间最大值。


def _seq_diff(later: int, earlier: int) -> int:
    """计算 15-bit 环形序号距离。

    Args:
        later: 较新的序号。
        earlier: 较旧的序号。

    Returns:
        modulo 32768 的前向距离。
    """
    return (later - earlier) & MAX_SEQ


# ==========================================================================
# SequenceNumbers
# ==========================================================================


@dataclass
class SequenceNumbers:
    """单 session 的 I-frame N(S)/N(R) 15-bit 环形计数器。"""

    _send_seq: int = field(default=0, init=False)
    _recv_seq: int = field(default=0, init=False)

    @property
    def send_seq(self) -> int:
        """下一个可用 N(S)。"""
        return self._send_seq

    @property
    def recv_seq(self) -> int:
        """下一帧期望的 N(R)。"""
        return self._recv_seq

    def next_send(self) -> int:
        """返回当前 N(S)，随后按 modulo 32768 递增。"""
        current = self._send_seq
        self._send_seq = (current + 1) & MAX_SEQ
        return current

    def next_recv(self) -> int:
        """返回当前 N(R)，随后按 modulo 32768 递增。"""
        current = self._recv_seq
        self._recv_seq = (current + 1) & MAX_SEQ
        return current

    def checkpoint(self) -> tuple[int, int]:
        """返回当前 (N(S), N(R)) 快照。"""
        return (self._send_seq, self._recv_seq)

    def recv_seq_for_ack(self) -> int:
        """返回出站 I/S-frame 应携带的 N(R)，即下一帧期望接收序号。"""
        return self._recv_seq


# ==========================================================================
# FlowController
# ==========================================================================


@dataclass
class FlowController:
    """IEC104 k/w 窗口控制器。

    k 限制未确认的出站 I-frame 数；w 限制未确认的入站 I-frame 数。
    本对象只维护计数，不负责实际等待、发送 S-frame 或 timer 调度。
    """

    k: int = 12
    """发送窗口 k。"""

    w: int = 8
    """接收窗口 w。"""

    _seq: SequenceNumbers = field(default_factory=SequenceNumbers, init=False)
    _acked_up_to: int = field(default=0, init=False)
    """对端最近确认到的发送序号。"""

    _unacked_recv_count: int = field(default=0, init=False)
    """尚未向对端确认的入站 I-frame 数。"""

    # ------------------------------------------------------------------
    # 发送方向流控
    # ------------------------------------------------------------------

    @property
    def can_send(self) -> bool:
        """发送窗口尚未满时为 True。"""
        return _seq_diff(self._seq.send_seq, self._acked_up_to) < self.k

    @property
    def outstanding(self) -> int:
        """返回尚未确认的出站 I-frame 数。"""
        return _seq_diff(self._seq.send_seq, self._acked_up_to)

    def next_send_seq(self) -> int:
        """原子获取并递增下一个 N(S)。"""
        return self._seq.next_send()

    def on_sent(self) -> None:
        """兼容旧调用的发送后递增接口。

        Notes:
            已废弃；新代码应使用 next_send_seq，避免取值与递增分离。
        """
        self._seq.next_send()

    def on_ack(self, recv_seq: int) -> None:
        """处理对端携带的 N(R) 确认。

        Args:
            recv_seq: 对端确认的下一期待序号；此前所有出站 I-frame 均视为已确认。
        """
        self._acked_up_to = recv_seq & MAX_SEQ

    def ack_is_outstanding(self) -> bool:
        """存在未确认出站 I-frame 时为 True。"""
        return _seq_diff(self._seq.send_seq, self._acked_up_to) > 0

    # ------------------------------------------------------------------
    # 接收方向流控
    # ------------------------------------------------------------------

    @property
    def needs_ack(self) -> bool:
        """未确认入站 I-frame 数达到 w 时为 True，调用方应尽快发送 S-frame。"""
        return self._unacked_recv_count >= self.w

    def on_received(self) -> None:
        """收到合法 I-frame 后推进 N(R) 并累计待确认数。"""
        self._seq.next_recv()
        self._unacked_recv_count += 1

    def on_ack_sent(self) -> None:
        """发送确认后清零入站待确认计数。"""
        self._unacked_recv_count = 0

    def recv_seq_for_ack(self) -> int:
        """返回出站 S/I-frame 应携带的 N(R)。"""
        return self._seq.recv_seq_for_ack()
