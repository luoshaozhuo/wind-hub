"""线性内存语义的 ADS 测试服务（真实 AMS/TCP 协议栈）。

pyads 自带 ``AdvancedHandler`` 以独立 Symbol 为单位存储：每个 index 地址
对应一个定长变量，Sum Read 按变量全长返回，Block Notification 只能覆盖
单个变量，无法表达真实 PLC「同一 IndexGroup 内连续字节内存 + 区块通知
跨多个测点」的语义。本模块在 pyads ``AdsTestServer``（真实 AMS/TCP
帧栈，属于 ``real_service``，不是 mock）之上提供：

- 每个 IndexGroup 一块连续字节内存，READ / WRITE / Sum Read / Sum Write
  都按字节偏移寻址，允许空洞和区块跨度；
- ADD/DELETE_DEVICE_NOTIFICATION 按字节区间注册；测试侧写入区间内任意
  字节即按注册长度投递原始字节块（与真实 PLC 的区块通知路径一致）；
- ADS State 可在 RUN/STOP 间切换，用于协议层故障测试；
- 服务端 Notification 注册表可直接观测，用于验证客户端异常退出后的
  PLC 侧句柄残留。

注意：与 pyads 官方 testserver 相同，通知数据面经进程内 ``callback_store``
触发（注册/注销命令仍走真实 TCP）；跨进程客户端的数据投递必须在真实
TwinCAT 上验收。
"""

from __future__ import annotations

import ctypes
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

#: pyads 客户端固定连接 AMS 路由器 TCP 端口（851/801 只是逻辑端口）。
ADS_TCP_PORT = 48898

_ERR_INVALID_OFFSET = 1808  # SYMBOL_NOT_FOUND，testserver 对未知地址的等价错误


@dataclass(frozen=True, slots=True)
class _Registration:
    """服务端一条 Device Notification 注册。"""

    handle: int
    group: int
    offset: int
    size: int


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

    # ------------------------------------------------------------------
    # 测试侧观测与控制 API（由 server 线程之外调用，全部加锁）
    # ------------------------------------------------------------------

    def set_ads_state(self, ads_state: int, device_state: int = 0) -> None:
        with self._lock:
            self._ads_state = ads_state
            self._device_state = device_state

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

    def reset_runtime(self) -> None:
        """模拟 PLC 重启：内存与 Notification 注册表全部清空。"""
        with self._lock:
            self._buffers.clear()
            self._registrations.clear()
            self._next_handle = 1
            self._ads_state = constants.ADSSTATE_RUN
            self._device_state = 0

    def write_memory(self, group: int, offset: int, data: bytes) -> None:
        """写入连续内存并触发与写入区间相交的区块通知。"""
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
    # pyads Handler 协议
    # ------------------------------------------------------------------

    def handle_request(self, request: AmsPacket) -> AmsResponseData:
        command_id = struct.unpack("<H", request.ams_header.command_id)[0]
        state = struct.unpack("<H", request.ams_header.state_flags)[0]
        state = struct.pack("<H", state | 0x0001)
        self.request_count += 1

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
        group, offset, size = struct.unpack_from("<III", data)
        with self._lock:
            handle = self._next_handle
            self._next_handle += 1
            self._registrations[handle] = _Registration(handle, group, offset, size)
            buffer = self._buffer_locked(group)
            if len(buffer) < offset + size:
                buffer.extend(b"\x00" * (offset + size - len(buffer)))
        return struct.pack("<I", handle)

    def _del_notification(self, data: bytes) -> bytes:
        handle = struct.unpack_from("<I", data)[0]
        with self._lock:
            self._registrations.pop(handle, None)
        return b""

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
        server = AdsTestServer(
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

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()
            with _suppress_runtime():
                server.join(timeout=5.0)

    def restart(self, *, reset: bool = True) -> None:
        """停止并重新监听同一地址；默认同时清空内存与通知注册表。"""
        self.stop()
        if reset:
            self.handler.reset_runtime()
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


class _suppress_runtime:
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
