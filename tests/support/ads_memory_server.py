"""线性内存语义的 ADS 测试服务（真实 AMS/TCP 协议栈）。

pyads 自带 ``AdvancedHandler`` 以独立 Symbol 为单位存储：每个 index 地址
对应一个定长变量，Sum Read 按变量全长返回，Block Notification 只能覆盖
单个变量，无法表达真实 PLC「同一 IndexGroup 内连续字节内存 + 区块通知
跨多个测点」的语义。本模块在 pyads ``AdsTestServer``（真实 AMS/TCP
帧栈，属于 ``real_service``，不是 mock）之上提供：

- 每个 IndexGroup 一块连续字节内存，READ / WRITE / Sum Read / Sum Write
  都按字节偏移寻址，允许空洞和区块跨度；
- ADD_DEVICE_NOTIFICATION 完整解析传输模式 / 周期 / 最大延迟 / 通知长度：
  ``ADSTRANS_SERVERCYCLE`` 注册项由单调时钟驱动的单调度线程按各自注册
  周期独立节拍（值不变也投递），``ADSTRANS_SERVERONCHA`` 保持写入变化
  即投递，其余传输模式不主动投递；
- ADS State 可在 RUN/STOP 间切换：STOP 期间暂停周期投递，恢复 RUN 后
  按新节拍继续（不补发 STOP 期间数据）；
- 每条 TCP 客户端连接、客户端 AMS 身份与 Notification Handle 分别记录，
  用于验证客户端异常退出（SIGKILL）后服务端句柄是否残留以及新客户端
  是否复用旧句柄；
- 服务端注册表/连接表可直接观测。

注意（数据面边界）：与 pyads 官方 testserver 相同，ADD/DELETE 通知等
**控制面命令走真实 AMS/TCP**，但通知**数据面经进程内 ``callback_store``
触发**——testserver 不重建客户端 AmsAddr，也不向其他 TCP 连接主动推送
DEVICE_NOTIFICATION 帧。因此跨进程客户端的通知投递（只能观测注册残留）
不等于跨进程 ADS Notification 验证，后者必须在真实 TwinCAT 上验收。
"""

from __future__ import annotations

import ctypes
import select
import socket
import struct
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pyads import constants, structs  # type: ignore[import-untyped]
from pyads.filetimes import dt_to_filetime  # type: ignore[import-untyped]
from pyads.pyads_ex import callback_store  # type: ignore[import-untyped]
from pyads.testserver import AdsTestServer  # type: ignore[import-untyped]
from pyads.testserver.handler import (  # type: ignore[import-untyped]
    AmsPacket,
    AmsResponseData,
)
from pyads.testserver.testserver import AdsClientConnection  # type: ignore[import-untyped]

#: pyads 客户端固定连接 AMS 路由器 TCP 端口（851/801 只是逻辑端口）。
ADS_TCP_PORT = 48898

_ERR_INVALID_OFFSET = 1808  # SYMBOL_NOT_FOUND，testserver 对未知地址的等价错误

#: SERVERCYCLE 周期下限：防止异常 0 周期造成调度空转。
_MIN_CYCLE_SECONDS = 0.001

#: 无到期注册项时调度线程的最长休眠（注销/停机由 Event 立即唤醒）。
_IDLE_SLEEP_SECONDS = 1.0


@dataclass(slots=True)
class _Registration:
    """服务端一条 Device Notification 注册。"""

    handle: int
    group: int
    offset: int
    size: int
    trans_mode: int
    cycle_time: float  # 秒；SERVERCYCLE 节拍周期
    max_delay: float  # 秒；仅记录观测
    conn_id: int | None
    source_ams: str
    next_due: float  # monotonic 下次到期时刻


@dataclass(slots=True)
class _ConnectionInfo:
    """一条 TCP 客户端连接的服务端观测记录（断开后保留用于残留分析）。"""

    conn_id: int
    peer: tuple[str, int] | None
    source_ams: str | None = None
    alive: bool = True


class LinearMemoryHandler:
    """按 IndexGroup 连续字节内存处理 ADS 命令的 Handler。

    不继承 ``AdvancedHandler``：其 ``handle_request`` 是闭包式实现，无法
    只覆盖 Sum/Notification 分支；命令分派帧格式与其完全一致。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffers: dict[int, bytearray] = {}
        self._registrations: dict[int, _Registration] = {}
        self._next_handle = 1
        self._ads_state: int = constants.ADSSTATE_RUN
        self._device_state: int = 0
        self.request_count = 0
        # 连接跟踪由 _TrackingClientConnection 在其自身线程内绑定。
        self._tls = threading.local()
        self._connections: dict[int, _ConnectionInfo] = {}
        # SERVERCYCLE 单调度线程：随首个周期注册惰性启动，stop 时回收，
        # 注册/注销不额外创建线程。
        self._scheduler: threading.Thread | None = None
        self._scheduler_wake = threading.Event()
        self._scheduler_shutdown = False

    # ------------------------------------------------------------------
    # 测试侧观测与控制 API（由 server 线程之外调用，全部加锁）
    # ------------------------------------------------------------------

    def set_ads_state(self, ads_state: int, device_state: int = 0) -> None:
        with self._lock:
            self._ads_state = ads_state
            self._device_state = device_state
        self._scheduler_wake.set()

    def set_running(self, running: bool) -> None:
        self.set_ads_state(
            constants.ADSSTATE_RUN if running else constants.ADSSTATE_STOP
        )

    def notification_count(self) -> int:
        with self._lock:
            return len(self._registrations)

    def registrations(self) -> tuple[_Registration, ...]:
        with self._lock:
            return tuple(self._registrations.values())

    def allocated_handles(self) -> int:
        """服务端累计分配的 Notification Handle 数量（单调不减）。"""
        with self._lock:
            return self._next_handle - 1

    def active_connection_count(self) -> int:
        with self._lock:
            return sum(1 for info in self._connections.values() if info.alive)

    def connections(self) -> tuple[_ConnectionInfo, ...]:
        with self._lock:
            return tuple(self._connections.values())

    def residue_registrations(self) -> tuple[_Registration, ...]:
        """所属 TCP 连接已断开但仍未注销的注册（SIGKILL 残留观测）。"""
        with self._lock:
            return tuple(
                reg
                for reg in self._registrations.values()
                if (info := self._connections.get(reg.conn_id)) is not None
                and not info.alive
            )

    def scheduler_alive(self) -> bool:
        with self._lock:
            return self._scheduler is not None and self._scheduler.is_alive()

    def scheduler_ident(self) -> int | None:
        """当前调度线程身份（注册/注销不应导致线程重建）。"""
        with self._lock:
            return self._scheduler.ident if self._scheduler is not None else None

    def reset_runtime(self) -> None:
        """模拟 PLC 重启：内存与 Notification 注册表全部清空。

        调度线程不销毁（与 handler 同生命周期），只清空节拍并解除停机
        标志，使 AdsMemoryServer.restart 后可继续服务。
        """
        with self._lock:
            self._buffers.clear()
            self._registrations.clear()
            self._next_handle = 1
            self._ads_state = constants.ADSSTATE_RUN
            self._device_state = 0
            self._scheduler_shutdown = False
        self._scheduler_wake.set()

    def shutdown_scheduler(self) -> None:
        """服务最终停止：终止并回收周期调度线程。"""
        with self._lock:
            self._scheduler_shutdown = True
            thread = self._scheduler
        self._scheduler_wake.set()
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            if self._scheduler is thread:
                self._scheduler = None

    def write_memory(self, group: int, offset: int, data: bytes) -> None:
        """写入连续内存；仅 SERVERONCHA 注册在数据变化时立即投递。"""
        fired: list[tuple[int, bytes]] = []
        with self._lock:
            buffer = self._buffer_locked(group)
            end = offset + len(data)
            if len(buffer) < end:
                buffer.extend(b"\x00" * (end - len(buffer)))
            changed = buffer[offset:end] != data
            buffer[offset:end] = data
            if changed:
                for reg in self._registrations.values():
                    if reg.trans_mode != constants.ADSTRANS_SERVERONCHA:
                        continue
                    if reg.group != group:
                        continue
                    reg_end = reg.offset + reg.size
                    if reg_end <= offset or reg.offset >= end:
                        continue
                    payload = bytes(buffer[reg.offset:reg_end])
                    if len(payload) == reg.size:
                        fired.append((reg.handle, payload))
        for handle, payload in fired:
            self._fire_notification(handle, payload)

    def read_memory(self, group: int, offset: int, size: int) -> bytes | None:
        with self._lock:
            buffer = self._buffers.get(group)
            if buffer is None or offset + size > len(buffer):
                return None
            return bytes(buffer[offset:offset + size])

    # ------------------------------------------------------------------
    # TCP 连接跟踪（由 _TrackingClientConnection 在连接线程内调用）
    # ------------------------------------------------------------------

    def bind_connection(self, conn_id: int, peer: tuple[str, int] | None) -> None:
        with self._lock:
            self._connections[conn_id] = _ConnectionInfo(conn_id, peer)

    def unbind_connection(self, conn_id: int) -> None:
        with self._lock:
            info = self._connections.get(conn_id)
            if info is not None:
                info.alive = False
            # 不删除该连接的注册项：testserver 不在 TCP 断开时回收句柄，
            # 正是 SIGKILL 残留观测要记录的行为。

    def _bind_current_thread(self, conn_id: int | None) -> None:
        self._tls.conn_id = conn_id

    # ------------------------------------------------------------------
    # pyads Handler 协议
    # ------------------------------------------------------------------

    def handle_request(self, request: AmsPacket) -> AmsResponseData:
        command_id = struct.unpack("<H", request.ams_header.command_id)[0]
        state = struct.unpack("<H", request.ams_header.state_flags)[0]
        state = struct.pack("<H", state | 0x0001)
        self.request_count += 1

        conn_id = getattr(self._tls, "conn_id", None)
        source_ams = _format_source_ams(request.ams_header)
        with self._lock:
            if conn_id is not None:
                info = self._connections.get(conn_id)
                if info is not None:
                    info.source_ams = source_ams

        handlers = {
            constants.ADSCOMMAND_READDEVICEINFO: self._read_device_info,
            constants.ADSCOMMAND_READ: self._read,
            constants.ADSCOMMAND_WRITE: self._write,
            constants.ADSCOMMAND_READWRITE: self._read_write,
            constants.ADSCOMMAND_READSTATE: self._read_state,
            constants.ADSCOMMAND_WRITECTRL: lambda _d: b"",
            constants.ADSCOMMAND_ADDDEVICENOTE: self._add_notification,
            constants.ADSCOMMAND_DELDEVICENOTE: self._del_notification,
            constants.ADSCOMMAND_DEVICENOTE: lambda _d: b"",
        }
        handler = handlers.get(command_id)
        if handler is None:
            return AmsResponseData(
                state, request.ams_header.error_code, struct.pack("<I", 8)
            )
        try:
            content = handler(request.ams_header.data)
        except _AdsRequestError as exc:
            content = b""
            result = exc.code
        else:
            result = 0
        # 与 AdvancedHandler 一致：AMS 头 error 回显请求，命令结果码
        # （4 字节）作为 data 前缀。
        return AmsResponseData(
            state,
            request.ams_header.error_code,
            struct.pack("<I", result) + content,
        )

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------

    def _read_device_info(self, _data: bytes) -> bytes:
        # 与 AdvancedHandler 相同的版本/名字节布局。
        return b"\x01" + b"\x02" + b"\x03\x00" + b"AdsMemoryServer\x00"

    def _read_state(self, _data: bytes) -> bytes:
        with self._lock:
            return struct.pack("<HH", self._ads_state, self._device_state)

    def _read(self, data: bytes) -> bytes:
        group, offset, size = struct.unpack_from("<III", data)
        with self._lock:
            buffer = self._buffers.get(group)
            if buffer is None or offset >= len(buffer):
                raise _AdsRequestError(_ERR_INVALID_OFFSET)
            # STRING 读取按固定缓冲（pyads 为 1024 字节）请求，真实 PLC
            # 按实际内容长度响应；标量类型请求宽度与内存宽度一致。
            payload = bytes(buffer[offset:offset + size])
        return struct.pack("<I", len(payload)) + payload

    def _write(self, data: bytes) -> bytes:
        group, offset, size = struct.unpack_from("<III", data)
        payload = data[12:12 + size]
        self.write_memory(group, offset, payload)
        return b""

    def _read_write(self, data: bytes) -> bytes:
        group = struct.unpack_from("<I", data)[0]
        index_offset = struct.unpack_from("<I", data, 4)[0]
        write_length = struct.unpack_from("<I", data, 12)[0]
        write_data = data[16:16 + write_length]

        if group == constants.ADSIGRP_SUMUP_READ:
            content = self._sum_read(index_offset, write_data)
        elif group == constants.ADSIGRP_SUMUP_WRITE:
            content = self._sum_write(index_offset, write_data)
        else:
            raise _AdsRequestError(_ERR_INVALID_OFFSET)
        return struct.pack("<I", len(content)) + content

    def _sum_read(self, count: int, write_data: bytes) -> bytes:
        items = [
            struct.unpack_from("<III", write_data, index * 12)
            for index in range(count)
        ]
        errors = bytearray(4 * count)
        payload = bytearray()
        with self._lock:
            for index, (group, offset, size) in enumerate(items):
                buffer = self._buffers.get(group)
                if buffer is None or offset + size > len(buffer):
                    struct.pack_into("<I", errors, index * 4, _ERR_INVALID_OFFSET)
                    payload.extend(b"\x00" * size)
                else:
                    payload.extend(buffer[offset:offset + size])
        return bytes(errors) + bytes(payload)

    def _sum_write(self, count: int, write_data: bytes) -> bytes:
        items = [
            struct.unpack_from("<III", write_data, index * 12)
            for index in range(count)
        ]
        data = write_data[count * 12:]
        results = bytearray(4 * count)
        cursor = 0
        writes: list[tuple[int, int, bytes]] = []
        for index, (group, offset, size) in enumerate(items):
            chunk = data[cursor:cursor + size]
            cursor += size
            writes.append((group, offset, chunk))
            if len(chunk) != size:
                struct.pack_into("<I", results, index * 4, _ERR_INVALID_OFFSET)
        for group, offset, chunk in writes:
            self.write_memory(group, offset, chunk)
        return bytes(results)

    def _add_notification(self, data: bytes) -> bytes:
        # 与 AdvancedHandler 相同的帧布局：group/offset/length/mode/
        # max_delay/cycle_time 各 4 字节；时间单位为 100ns。
        if len(data) < 24:
            raise _AdsRequestError(_ERR_INVALID_OFFSET)
        group, offset, size, trans_mode, max_delay_raw, cycle_raw = (
            struct.unpack_from("<IIIIII", data)
        )
        now = time.monotonic()
        cycle_seconds = max(cycle_raw / 1e7, _MIN_CYCLE_SECONDS)
        max_delay_seconds = max_delay_raw / 1e7
        with self._lock:
            handle = self._next_handle
            self._next_handle += 1
            conn_id = getattr(self._tls, "conn_id", None)
            source_ams = ""
            if conn_id is not None:
                info = self._connections.get(conn_id)
                if info is not None and info.source_ams is not None:
                    source_ams = info.source_ams
            self._registrations[handle] = _Registration(
                handle=handle,
                group=group,
                offset=offset,
                size=size,
                trans_mode=trans_mode,
                cycle_time=cycle_seconds,
                max_delay=max_delay_seconds,
                conn_id=conn_id,
                source_ams=source_ams,
                next_due=now + cycle_seconds,
            )
            buffer = self._buffer_locked(group)
            if len(buffer) < offset + size:
                buffer.extend(b"\x00" * (offset + size - len(buffer)))
            if trans_mode == constants.ADSTRANS_SERVERCYCLE:
                self._ensure_scheduler_locked()
        self._scheduler_wake.set()
        return struct.pack("<I", handle)

    def _del_notification(self, data: bytes) -> bytes:
        handle = struct.unpack_from("<I", data)[0]
        with self._lock:
            self._registrations.pop(handle, None)
        self._scheduler_wake.set()
        return b""

    # ------------------------------------------------------------------
    # SERVERCYCLE 调度
    # ------------------------------------------------------------------

    def _ensure_scheduler_locked(self) -> None:
        """调用方持锁；惰性启动唯一的周期调度线程。"""
        if self._scheduler_shutdown:
            return
        if self._scheduler is None or not self._scheduler.is_alive():
            thread = threading.Thread(
                target=self._run_scheduler,
                name="ads-memory-cycle-scheduler",
                daemon=True,
            )
            self._scheduler = thread
            thread.start()

    def _run_scheduler(self) -> None:
        """单线程按各注册项独立周期投递；Event 等待，绝不忙轮询。"""
        while True:
            # clear 必须在扫描之前：扫描与等待之间的注册/注销/状态切换
            # 置位 Event 不会丢失。
            self._scheduler_wake.clear()
            fired: list[tuple[int, bytes]] = []
            with self._lock:
                if self._scheduler_shutdown:
                    return
                now = time.monotonic()
                running = self._ads_state == constants.ADSSTATE_RUN
                wait_seconds = _IDLE_SLEEP_SECONDS
                for reg in self._registrations.values():
                    if reg.trans_mode != constants.ADSTRANS_SERVERCYCLE:
                        continue
                    if now < reg.next_due:
                        delay = reg.next_due - now
                    elif running:
                        buffer = self._buffers.get(reg.group)
                        end = reg.offset + reg.size
                        if buffer is not None and len(buffer) >= end:
                            fired.append(
                                (reg.handle, bytes(buffer[reg.offset:end]))
                            )
                        # 长停顿（STOP/调度延迟）后不补发历史节拍。
                        advanced = reg.next_due + reg.cycle_time
                        reg.next_due = (
                            advanced if advanced >= now else now + reg.cycle_time
                        )
                        delay = reg.cycle_time
                    else:
                        # Runtime STOP：暂停节拍；恢复 RUN 后按当前时刻
                        # 重新排期，不批量补发。
                        reg.next_due = now + reg.cycle_time
                        delay = reg.cycle_time
                    if delay < wait_seconds:
                        wait_seconds = delay
            # 用户回调在锁外触发，避免回调内再发 ADS 命令时自锁。
            for handle, payload in fired:
                self._fire_notification(handle, payload)
            self._scheduler_wake.wait(timeout=max(wait_seconds, 0.0))

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _buffer_locked(self, group: int) -> bytearray:
        buffer = self._buffers.get(group)
        if buffer is None:
            buffer = bytearray()
            self._buffers[group] = buffer
        return buffer

    def _fire_notification(self, handle: int, payload: bytes) -> None:
        """经 pyads 进程内 callback_store 投递原始字节块。

        与 ``PLCVariable.write`` 相同的投递路径：按 handle 匹配客户端
        注册的 C 回调，由 pyads 内部线程机制转交给用户回调。
        """
        header = structs.SAdsNotificationHeader()
        header.hNotification = handle
        header.nTimeStamp = dt_to_filetime(datetime.now().astimezone())
        header.cbSampleSize = len(payload)
        target = ctypes.addressof(header) + structs.SAdsNotificationHeader.data.offset
        ctypes.memmove(target, payload, len(payload))
        # 与 AdvancedHandler.PLCVariable.write 相同的投递形式：testserver
        # 不重建客户端 AmsAddr，直接按 notification handle 匹配进程内回调。
        for (ams_addr, notification_handle), func in tuple(callback_store.items()):
            if notification_handle == handle:
                func(ams_addr.amsAddrStruct(), header, 0)


def _format_source_ams(ams_header: Any) -> str:
    """把请求帧源 AMS 地址格式化为 'net-id:port'。"""
    net_id = ams_header.source_net_id
    port = struct.unpack("<H", ams_header.source_port)[0]
    return f"{'.'.join(str(byte) for byte in net_id)}:{port}"


class _TrackingClientConnection(AdsClientConnection):
    """在 pyads 连接线程上绑定/解绑 handler 侧连接身份。"""

    def __init__(self, *args: Any, conn_id: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._wh_conn_id = conn_id

    def run(self) -> None:
        handler = self.handler
        try:
            peer: tuple[str, int] | None = self.client.getpeername()
        except OSError:
            peer = None
        handler.bind_connection(self._wh_conn_id, peer)
        handler._bind_current_thread(self._wh_conn_id)
        try:
            super().run()
        finally:
            handler._bind_current_thread(None)
            handler.unbind_connection(self._wh_conn_id)


class _TrackingAdsTestServer(AdsTestServer):
    """AdsTestServer 的连接工厂不可注入：复制 accept 循环以使用跟踪连接。

    循环体与 pyads 3.6 的实现保持一致，仅替换连接类。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._wh_next_conn_id = 0

    def run(self) -> None:
        self._run = True
        self.server.listen(5)
        while self._run:
            ready, _, _ = select.select([self.server], [], [], 0.1)
            if not ready:
                continue
            try:
                client, address = self.server.accept()
            except OSError:
                continue
            self._wh_next_conn_id += 1
            client_thread = _TrackingClientConnection(
                handler=self.handler,
                client=client,
                address=address,
                server=self,
                conn_id=self._wh_next_conn_id,
            )
            client_thread.daemon = True
            client_thread.start()
            self.clients.append(client_thread)


class _AdsRequestError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"ADS request error {code}")
        self.code = code


class AdsMemoryServer:
    """绑定 loopback 地址的线性内存 ADS 测试服务（含生命周期管理）。"""

    def __init__(
        self,
        *,
        ip_address: str = "127.0.0.1",
        handler: LinearMemoryHandler | None = None,
    ) -> None:
        self.ip_address = ip_address
        self.handler = handler or LinearMemoryHandler()
        self._server: AdsTestServer | None = None

    def start(self, *, timeout: float = 5.0) -> None:
        server = _TrackingAdsTestServer(
            handler=self.handler,
            ip_address=self.ip_address,
            port=ADS_TCP_PORT,
            logging=False,
        )
        server.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.ip_address, ADS_TCP_PORT), 0.2):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            server.stop()
            raise RuntimeError(f"ADS memory server did not open {self.ip_address}")
        self._server = server

    def _stop_tcp(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()
            with _SuppressRuntime():
                server.join(timeout=5.0)

    def stop(self) -> None:
        """停止 TCP 服务并回收调度线程（最终停机）。"""
        self._stop_tcp()
        self.handler.shutdown_scheduler()

    def restart(self, *, reset: bool = True) -> None:
        """停止并重新监听同一地址；默认同时清空内存与通知注册表。"""
        # 先清注册表再停 TCP，避免停窗内调度线程投递旧句柄通知。
        if reset:
            self.handler.reset_runtime()
        self._stop_tcp()
        time.sleep(0.05)  # 等待监听 socket 完成关闭
        self.start()

    def set_values(self, group: int, offset: int, data: bytes) -> None:
        self.handler.write_memory(group, offset, data)

    def set_ads_state(self, ads_state: int, device_state: int = 0) -> None:
        self.handler.set_ads_state(ads_state, device_state)

    def set_running(self, running: bool) -> None:
        self.handler.set_running(running)

    def notification_count(self) -> int:
        return self.handler.notification_count()

    def registrations(self) -> tuple[Any, ...]:
        return self.handler.registrations()

    def connections(self) -> tuple[Any, ...]:
        return self.handler.connections()

    def active_connection_count(self) -> int:
        return self.handler.active_connection_count()

    def residue_registrations(self) -> tuple[Any, ...]:
        return self.handler.residue_registrations()

    def allocated_handles(self) -> int:
        return self.handler.allocated_handles()

    def scheduler_alive(self) -> bool:
        return self.handler.scheduler_alive()

    def scheduler_ident(self) -> int | None:
        return self.handler.scheduler_ident()


class _SuppressRuntime:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        del exc
        return True


def pack_points(values: Sequence[tuple[str, Any]]) -> bytes:
    """按 (ADS 类型名, 值) 序列化为连续小端字节，便于构造区块内存。"""
    formats = {
        "BOOL": "?",
        "SINT": "b",
        "USINT": "B",
        "INT": "h",
        "UINT": "H",
        "DINT": "i",
        "UDINT": "I",
        "LINT": "q",
        "ULINT": "Q",
        "REAL": "f",
        "LREAL": "d",
    }
    return b"".join(struct.pack("<" + formats[k], v) for k, v in values)
