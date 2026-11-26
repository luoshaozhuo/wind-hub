"""真实 DCEMS WebSocket 动态观测，不以 HTTP 101 或页面静态文件替代数据。"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Any


def _read_exact(connection: socket.socket, length: int) -> bytes:
    payload = bytearray()
    while len(payload) < length:
        chunk = connection.recv(length - len(payload))
        if not chunk:
            raise ConnectionError("WebSocket closed before full frame")
        payload.extend(chunk)
    return bytes(payload)


def _frame(connection: socket.socket) -> dict[str, Any]:
    first, second = _read_exact(connection, 2)
    opcode = first & 15
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", _read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(connection, 8))[0]
    if second & 128:
        raise ValueError("Server-to-client WebSocket frames must not be masked")
    body = _read_exact(connection, length)
    if opcode == 8:
        raise ConnectionError("DCEMS WebSocket closed")
    if opcode != 1:
        return {}
    result = json.loads(body.decode("utf-8"))
    return result if isinstance(result, dict) else {}


def _send_text(connection: socket.socket, value: dict[str, Any]) -> None:
    """WebSocket client frames must be masked (RFC 6455)."""
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    mask = os.urandom(4)
    if len(payload) < 126:
        head = bytes((0x81, 0x80 | len(payload)))
    elif len(payload) <= 65535:
        head = bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(payload))
    else:
        raise ValueError("unexpectedly large subscription")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    connection.sendall(head + mask + masked)


def observe(
    host: str,
    port: int,
    *,
    seconds: float = 14.0,
    keys: Sequence[str] | None = None,
) -> dict[str, set[str]]:
    """订阅真实 DCEMS 内存键；默认覆盖十机风速、工作模式及站控/调频。"""
    if keys is None:
        keys = [f"WTG{index:02d}_emsWinSpd" for index in range(1, 11)]
        keys.extend(f"WTG{index:02d}_emsWorkMode" for index in range(1, 11))
        keys.extend(
            [
                "SUBSTATION_powAct",
                "SUBSTATION_powReact",
                "PFR_isFreMode",
            ]
        )
    else:
        keys = list(keys)
    if not keys:
        raise ValueError("at least one DCEMS key is required")
    conn = socket.create_connection((host, port), timeout=6)
    try:
        conn.settimeout(6)
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET /api/ws HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n\r\n"
        )
        conn.sendall(handshake.encode("ascii"))
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            fragment = conn.recv(1024)
            if not fragment:
                raise ConnectionError("WebSocket handshake interrupted")
            buffer.extend(fragment)
            if len(buffer) > 8192:
                raise ValueError("oversized HTTP WebSocket handshake")
        header, trailing = bytes(buffer).split(b"\r\n\r\n", 1)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket upgrade failed: {header[:180]!r}")
        if trailing:
            raise ValueError("unexpected response payload before subscription")
        _send_text(conn, {"type": "kv", "uid": "dcems-smoke", "keys": keys})
        observed: dict[str, set[str]] = defaultdict(set)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            frame = _frame(conn)
            values = frame.get("valMap")
            if isinstance(values, dict):
                for key in keys:
                    if key in values and values[key] is not None:
                        observed[key].add(str(values[key]))
            # 完整覆盖调用方窗口，避免提前退出造成协议采样与推送样本不重叠。
        return dict(observed)
    finally:
        conn.close()
