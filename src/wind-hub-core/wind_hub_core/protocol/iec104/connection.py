"""IEC104 单连接状态机。

状态机只表达 TCP/STARTDT 数据传输生命周期，不持有 socket，也不执行重连。
非法状态迁移抛 ValueError；FAILED 可由任意状态进入，恢复必须先显式回到
DISCONNECTED。
"""

from __future__ import annotations

from enum import Enum, auto
from typing import ClassVar


class ConnectionState(Enum):
    """IEC104 连接生命周期状态。"""

    DISCONNECTED = auto()
    """尚未建立 TCP 连接。"""

    TCP_CONNECTED = auto()
    """TCP 已连接，但尚未完成 STARTDT。"""

    STARTDT_PENDING = auto()
    """已发送 STARTDT_ACT，等待 STARTDT_CON。"""

    STARTED = auto()
    """数据传输已激活，可交换 I-frame。"""

    STOPPED = auto()
    """数据传输已停止。"""

    FAILED = auto()
    """连接进入失败态；必须由外部恢复流程重置。"""


class ConnectionStateMachine:
    """约束 IEC104 连接合法状态迁移。

    任意状态都可进入 FAILED；FAILED 只能先转到 DISCONNECTED 后再重新连接。
    """

    # 当前状态到允许目标状态的迁移表。
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
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """返回当前连接状态。"""
        return self._state

    @property
    def is_connected(self) -> bool:
        """判断当前状态是否意味着 TCP socket 已建立。"""
        return self._state in {
            ConnectionState.TCP_CONNECTED,
            ConnectionState.STARTDT_PENDING,
            ConnectionState.STARTED,
            ConnectionState.STOPPED,
        }

    @property
    def is_started(self) -> bool:
        """判断 STARTDT 数据传输是否已激活。"""
        return self._state == ConnectionState.STARTED

    # ------------------------------------------------------------------
    # 状态迁移
    # ------------------------------------------------------------------

    def transition(self, new_state: ConnectionState) -> None:
        """执行受约束的状态迁移。

        Args:
            new_state: 目标状态。

        Raises:
            ValueError: 当前状态不允许迁移到目标状态。
        """
        allowed = self._TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise ValueError(f"Illegal state transition: {self._state.name} → " f"{new_state.name}")
        self._state = new_state

    def to_disconnected(self) -> None:
        """迁移到 DISCONNECTED。"""
        self.transition(ConnectionState.DISCONNECTED)

    def to_tcp_connected(self) -> None:
        """迁移到 TCP_CONNECTED。"""
        self.transition(ConnectionState.TCP_CONNECTED)

    def to_startdt_pending(self) -> None:
        """迁移到 STARTDT_PENDING。"""
        self.transition(ConnectionState.STARTDT_PENDING)

    def to_started(self) -> None:
        """迁移到 STARTED。"""
        self.transition(ConnectionState.STARTED)

    def to_stopped(self) -> None:
        """迁移到 STOPPED。"""
        self.transition(ConnectionState.STOPPED)

    def to_failed(self) -> None:
        """无条件进入 FAILED，用于不可恢复协议错误。"""
        self._state = ConnectionState.FAILED  # FAILED 是异常收敛态，允许从任意状态进入。

    # ------------------------------------------------------------------
    # 调试表示
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"ConnectionStateMachine({self._state.name})"
