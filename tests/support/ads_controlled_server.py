"""并发实验用受控 ADS 内存服务器（真实 AMS/TCP 协议栈）。

``AdsMemoryServer`` 的 Handler 立即应答、无法构造慢响应/拒绝/强制重叠。
本模块在其线性内存语义之上增加**仅测试使用**的控制面：

- 按 ADS 命令注入「应答前延迟」（延迟发生在内存锁之外，且发生在服务端
  单连接线程上，用于放大客户端多线程同时在途的窗口）；
- 按命令返回指定 ADS Result 码（模拟服务端拒绝）；
- 按命令设置 ``threading.Barrier``：N 个请求同时到达分派点才放行，
  保证并发是真实重叠，而非线程启动时间重叠；
- 记录每条请求的 invoke id、命令、服务端在途重叠数、接收/应答时刻，
  作为 AMS 请求/响应关联的服务端证据；
- 服务端在途峰值（同一 TCP 连接线程模型下恒为 1，多连接时可 >1）。

控制面延迟/拒绝只在显式配置时生效；不配置时行为与 LinearMemoryHandler
完全一致。本模块是测试工具，不接入任何生产路径。
"""

from __future__ import annotations

import contextlib
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from pyads import constants  # type: ignore[import-untyped]
from pyads.testserver.handler import (  # type: ignore[import-untyped]
    AmsPacket,
    AmsResponseData,
)

from tests.support.ads_memory_server import (
    AdsMemoryServer,
    LinearMemoryHandler,
)


@dataclass(slots=True)
class RequestRecord:
    """服务端一条请求的观测记录（monotonic 秒）。"""

    command_id: int
    invoke_id: int
    t_recv: float
    t_resp: float
    active_at_dispatch: int
    rejected: bool = False
    server_thread: int = 0


@dataclass(slots=True)
class _CommandRule:
    delay: float = 0.0
    reject_code: int | None = None
    barrier: threading.Barrier | None = None
    calls: int = 0


class ControlledMemoryHandler(LinearMemoryHandler):
    """支持延迟/拒绝/屏障/请求取证的线性内存 Handler。"""

    def __init__(self, log_maxlen: int = 200_000) -> None:
        super().__init__()
        self._rules_lock = threading.Lock()
        self._rules: dict[int, _CommandRule] = {}
        self.request_log: deque[RequestRecord] = deque(maxlen=log_maxlen)
        # 入口到达记录：在延迟/拒绝规则应用**之前**记录，供取消测试确认命令
        # 已在途（request_count 在 super().handle_request 内才递增，太晚）。
        self.arrival_log: deque[tuple[int, int, float]] = deque(maxlen=log_maxlen)
        self._active_lock = threading.Lock()
        self._active_by_command: dict[int, int] = {}
        self._peak_by_command: dict[int, int] = {}

    # ------------------------------------------------------------------
    # 测试控制 API
    # ------------------------------------------------------------------

    def set_delay(self, command_id: int, seconds: float) -> None:
        with self._rules_lock:
            self._rule_locked(command_id).delay = max(seconds, 0.0)

    def set_reject(self, command_id: int, ads_result_code: int) -> None:
        with self._rules_lock:
            self._rule_locked(command_id).reject_code = ads_result_code

    def clear_reject(self, command_id: int) -> None:
        with self._rules_lock:
            rule = self._rules.get(command_id)
            if rule is not None:
                rule.reject_code = None

    def set_barrier(self, command_id: int, parties: int) -> threading.Barrier:
        """命令在分派前等待 N 方到齐；每次实验前调用 barrier.abort/reset 重建。"""
        barrier = threading.Barrier(parties)
        with self._rules_lock:
            self._rule_locked(command_id).barrier = barrier
        return barrier

    def clear_barrier(self, command_id: int) -> None:
        with self._rules_lock:
            rule = self._rules.get(command_id)
            if rule is not None:
                rule.barrier = None

    def reset_rules(self) -> None:
        with self._rules_lock:
            self._rules.clear()
        self.request_log.clear()
        self.arrival_log.clear()
        with self._active_lock:
            self._active_by_command.clear()
            self._peak_by_command.clear()

    @staticmethod
    def command_id(name: str) -> int:
        return int(getattr(constants, f"ADSCOMMAND_{name}"))

    def peak_active(self, command_id: int) -> int:
        with self._active_lock:
            return self._peak_by_command.get(command_id, 0)

    def records(self) -> tuple[RequestRecord, ...]:
        return tuple(self.request_log)

    def arrival_count(self, command_id: int) -> int:
        """命令到达入口的次数（含仍在延迟/拒绝窗口内未完成的）。"""
        return sum(1 for command, _invoke, _t in self.arrival_log if command == command_id)

    def reset_telemetry(self) -> None:
        """清空到达/完成/峰值记录，保留延迟/拒绝/屏障规则。

        connect 自身会发一条 READSTATE 做健康校验，各实验在连接建立后
        调用本方法归零，避免基线污染。
        """
        self.request_log.clear()
        self.arrival_log.clear()
        with self._active_lock:
            self._active_by_command.clear()
            self._peak_by_command.clear()

    def invoke_ids(self, command_id: int) -> tuple[int, ...]:
        return tuple(r.invoke_id for r in self.request_log if r.command_id == command_id)

    def _rule_locked(self, command_id: int) -> _CommandRule:
        rule = self._rules.get(command_id)
        if rule is None:
            rule = _CommandRule()
            self._rules[command_id] = rule
        return rule

    # ------------------------------------------------------------------
    # Handler 协议
    # ------------------------------------------------------------------

    def handle_request(self, request: AmsPacket) -> AmsResponseData:
        command_id = struct.unpack("<H", request.ams_header.command_id)[0]
        invoke_id = struct.unpack("<I", request.ams_header.invoke_id)[0]
        state_flags = struct.unpack("<H", request.ams_header.state_flags)[0]
        state = struct.pack("<H", state_flags | 0x0001)
        t_recv = time.monotonic()
        self.arrival_log.append((command_id, invoke_id, t_recv))

        with self._rules_lock:
            rule = self._rules.get(command_id)
            delay = rule.delay if rule is not None else 0.0
            reject_code = rule.reject_code if rule is not None else None
            barrier = rule.barrier if rule is not None else None
            if rule is not None:
                rule.calls += 1

        if barrier is not None:
            # 超时由实验方保证；BrokenBarrier 说明各方未按计划到齐。
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=10.0)

        if reject_code is not None:
            # 真实 PLC 的 READ 错误帧在结果码后仍带 4 字节数据长度（0）；
            # 仅回 4 字节结果码会被 AdsLib 判为响应损坏（1861 超时）。
            payload = struct.pack("<I", reject_code)
            if command_id == constants.ADSCOMMAND_READ:
                payload += struct.pack("<I", 0)
            response = AmsResponseData(
                state,
                request.ams_header.error_code,
                payload,
            )
            self._record(command_id, invoke_id, t_recv, active=0, rejected=True)
            return response

        if delay > 0.0:
            time.sleep(delay)

        with self._active_lock:
            active = self._active_by_command.get(command_id, 0) + 1
            self._active_by_command[command_id] = active
            self._peak_by_command[command_id] = max(
                self._peak_by_command.get(command_id, 0), active
            )
        try:
            response = super().handle_request(request)
        finally:
            with self._active_lock:
                self._active_by_command[command_id] -= 1
        self._record(command_id, invoke_id, t_recv, active=active, rejected=False)
        return response

    def _record(
        self,
        command_id: int,
        invoke_id: int,
        t_recv: float,
        *,
        active: int,
        rejected: bool,
    ) -> None:
        self.request_log.append(
            RequestRecord(
                command_id=command_id,
                invoke_id=invoke_id,
                t_recv=t_recv,
                t_resp=time.monotonic(),
                active_at_dispatch=active,
                rejected=rejected,
                server_thread=threading.get_ident(),
            )
        )


class ControlledAdsMemoryServer(AdsMemoryServer):
    """使用 ControlledMemoryHandler 的 AdsMemoryServer，暴露控制 API。"""

    def __init__(self, *, ip_address: str = "127.0.0.1") -> None:
        self.controlled_handler = ControlledMemoryHandler()
        super().__init__(ip_address=ip_address, handler=self.controlled_handler)

    @property
    def control(self) -> ControlledMemoryHandler:
        return self.controlled_handler


def command_name(command_id: int) -> str:
    """命令 id 转回 pyads 常量名（报告用）。"""
    for name in dir(constants):
        if name.startswith("ADSCOMMAND_") and getattr(constants, name) == command_id:
            return name.removeprefix("ADSCOMMAND_")
    return f"UNKNOWN({command_id})"


def server_telemetry(handler: Any) -> dict[str, Any]:
    """导出服务端取证数据（供子进程 JSON 输出）。"""
    records = handler.request_log if hasattr(handler, "request_log") else ()
    return {
        "request_count": int(getattr(handler, "request_count", 0)),
        "records": [
            {
                "command": (r.command_id if hasattr(r, "command_id") else r[0]),
                "invoke_id": (r.invoke_id if hasattr(r, "invoke_id") else r[1]),
                "t_recv": (r.t_recv if hasattr(r, "t_recv") else 0.0),
                "t_resp": (r.t_resp if hasattr(r, "t_resp") else 0.0),
                "active": (r.active_at_dispatch if hasattr(r, "active_at_dispatch") else 0),
                "server_thread": (r.server_thread if hasattr(r, "server_thread") else 0),
            }
            for r in tuple(records)
        ],
    }
