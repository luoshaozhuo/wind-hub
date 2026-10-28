"""IEC104 单连接资源所有者。唯一接收循环和物理关闭在此收敛。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from hat.drivers import iec104, net

from core.application.errors import ProtocolConnectionError

logger = logging.getLogger(__name__)

MessageHandler = Callable[[object], None]
DisconnectHandler = Callable[[BaseException], None]


class _IEC104Session:
    """一次性连接资源：关闭未完成时不允许上层创建替代会话。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        t1: float,
        t2: float,
        t3: float,
        k: int,
        w: int,
        on_message: MessageHandler,
        on_disconnect: DisconnectHandler,
    ) -> None:
        self._address = net.TcpAddress(host, port)
        self._options = {
            "response_timeout": t1,
            "supervisory_timeout": t2,
            "test_timeout": t3,
            "send_window_size": k,
            "receive_window_size": w,
        }
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._connection: iec104.Connection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._close_error: BaseException | None = None
        self._closing = False
        self._enabled_once = False
        self._enabled_event = asyncio.Event()
        self._enabled_handle: object | None = None

    @property
    def is_settled(self) -> bool:
        return (
            self._close_task is not None
            and self._close_task.done()
            and not self._close_task.cancelled()
            and self._close_task.exception() is None
        )

    @property
    def is_open(self) -> bool:
        conn = self._connection
        return conn is not None and not self._closing and conn.is_enabled

    async def open(self) -> None:
        if self._connection is not None or self._closing:
            raise ProtocolConnectionError("IEC104 session cannot be reopened")
        conn = await iec104.connect(addr=self._address, **self._options)
        if self._closing:
            await conn.async_close()
            raise ProtocolConnectionError("IEC104 session was closed during connect")
        self._connection = conn
        self._enabled_handle = conn.register_enabled_cb(self._on_enabled)
        if conn.is_enabled:
            self._on_enabled(True)
        self._receiver = asyncio.create_task(self._receive_loop(), name="iec104-receive")
        await self._enabled_event.wait()
        if self._closing:
            raise ProtocolConnectionError("IEC104 STARTDT disabled before session became ready")

    def _on_enabled(self, enabled: bool) -> None:
        if enabled:
            self._enabled_once = True
            self._enabled_event.set()
        elif self._enabled_once and not self._closing:
            self._closing = True
            try:
                self._on_disconnect(ProtocolConnectionError("IEC104 STOPDT received"))
            finally:
                self._request_close()

    async def send(self, messages: list[Any]) -> None:
        async with self._send_lock:
            if self._closing or self._connection is None or not self._connection.is_enabled:
                raise ProtocolConnectionError("IEC104 session is unavailable")
            await self._connection.send(messages)

    async def _receive_loop(self) -> None:
        try:
            while not self._closing:
                conn = self._connection
                if conn is None:
                    return
                try:
                    messages = await conn.receive()
                except iec104.AsduTypeError:
                    continue
                try:
                    self._on_message(messages)
                except Exception:
                    logger.exception("IEC104 application dispatch failed; dropping invalid APDU")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._closing = True
                try:
                    self._on_disconnect(exc)
                finally:
                    self._request_close()

    def _request_close(self) -> None:
        self._closing = True
        if self._close_task is None or (self._close_task.done() and self._close_error is not None):
            self._close_error = None
            self._close_task = asyncio.create_task(self._close_once())

    async def close(self) -> None:
        self._request_close()
        assert self._close_task is not None
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        try:
            async with self._send_lock:
                conn = self._connection
                if conn is not None:
                    await conn.async_close()
            task = self._receiver
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._receiver = None
            self._connection = None
        except BaseException as exc:
            self._close_error = exc
            raise
