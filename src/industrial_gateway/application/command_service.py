"""Gateway 控制命令转发：每点串行，UNKNOWN 闭锁，绝不自动重试。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from industrial_gateway.domain import PointWrite, Scalar

logger = logging.getLogger(__name__)

PointKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CommandTarget:
    device_id: str
    point_id: str
    timeout: float = 1.0
    rpc_timeout: float = 2.0

    def __post_init__(self) -> None:
        if (
            not self.device_id
            or not self.point_id
            or self.timeout <= 0
            or self.rpc_timeout <= self.timeout
        ):
            raise ValueError("invalid Commander target")


class CommandState(StrEnum):
    READY = "ready"
    EXECUTING = "executing"
    UNKNOWN = "unknown"


class CommandOutcome(StrEnum):
    CONFIRMED_SUCCESS = "confirmed_success"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CommandRequest:
    command_id: str
    device_id: str
    point_id: str
    value: Scalar
    timeout: float


@dataclass(frozen=True, slots=True)
class CommandResult:
    outcome: CommandOutcome
    error: str = ""


class CommanderUnavailableError(RuntimeError):
    """传输层无法确认命令执行结果。"""


class CommanderWriter(Protocol):
    async def write_point(
        self, request: CommandRequest, *, timeout: float
    ) -> CommandResult: ...


class CommandJournalPort(Protocol):
    """控制日志持久化能力；不泄露基础设施实现。"""

    def blocked(self, key: str) -> bool: ...

    def begin(self, key: str, command_id: str) -> None: ...

    def confirmed(self, key: str) -> None: ...


class CommanderProxy:
    """请求期等待真实结果；不将网络超时误判为确定失败。

    当前采用同步确认写语义，不引入无法可靠持久化的后台任务队列。
    接到 UNKNOWN 后闭锁该逻辑控制点；恢复必须人工处理。
    """

    def __init__(
        self,
        stub: CommanderWriter,
        targets: dict[PointKey, CommandTarget],
        journal: CommandJournalPort | None = None,
    ) -> None:
        self._stub = stub
        self._journal = journal
        self._targets = dict(targets)
        self._locks = {key: asyncio.Lock() for key in targets}
        self._states = {key: CommandState.READY for key in targets}

    def state(self, device_id: str, point_id: str) -> CommandState:
        return self._states[(device_id, point_id)]

    async def __call__(self, request: PointWrite) -> None:
        key = (request.device_id, request.point_id)
        target = self._targets.get(key)
        if target is None:
            raise PermissionError("command point has no Commander target")
        async with self._locks[key]:
            journal_key = json.dumps(key, separators=(",", ":"), ensure_ascii=False)
            if (self._journal is not None and self._journal.blocked(journal_key)) or self._states[
                key
            ] is CommandState.UNKNOWN:
                raise RuntimeError("command point locked after unknown outcome")
            self._validate_value(request.value)
            command_id = uuid4().hex
            if self._journal is not None:
                self._journal.begin(journal_key, command_id)
            self._states[key] = CommandState.EXECUTING
            command = CommandRequest(
                command_id=command_id,
                device_id=target.device_id,
                point_id=target.point_id,
                value=request.value,
                timeout=target.timeout,
            )
            try:
                result = await self._stub.write_point(command, timeout=target.rpc_timeout)
            except CommanderUnavailableError as exc:
                # RPC 断开/超时可能发生在现场已经执行之后。
                self._states[key] = CommandState.UNKNOWN
                logger.error(
                    "Gateway command UNKNOWN id=%s target=%s error=%s",
                    command_id,
                    key,
                    exc,
                )
                raise RuntimeError("Commander result unknown; control locked") from exc
            except BaseException:
                # 包含取消：如果请求已发出，必须按未知执行状态闭锁。
                self._states[key] = CommandState.UNKNOWN
                raise
            if result.outcome == CommandOutcome.CONFIRMED_SUCCESS:
                if self._journal is not None:
                    self._journal.confirmed(journal_key)
                self._states[key] = CommandState.READY
                return
            if result.outcome == CommandOutcome.CONFIRMED_FAILURE:
                if self._journal is not None:
                    self._journal.confirmed(journal_key)
                self._states[key] = CommandState.READY
                raise RuntimeError(f"Commander rejected command: {result.error}")
            self._states[key] = CommandState.UNKNOWN
            logger.error("Gateway command UNKNOWN id=%s target=%s", command_id, key)
            raise RuntimeError("Commander result unknown; control locked")

    @staticmethod
    def _validate_value(value: Scalar) -> None:
        if isinstance(value, int):
            if value < -(1 << 63):
                raise ValueError("integer below signed 64-bit range")
            if value > (1 << 64) - 1:
                raise ValueError("integer above unsigned 64-bit range")
