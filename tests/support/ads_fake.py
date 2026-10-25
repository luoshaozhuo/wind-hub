"""pyads Connection 的进程内替身与 ADS 驱动构造辅助。

新 ADSDriver 栈只通过 ``_AdsSession`` 调用 pyads Connection 的同步阻塞
API（``open/close/set_timeout/read_state/get_symbol/read_write`` 与
notification 系列）。本替身**不模拟 AMS/TCP**，用于确定性并发与生命周期
测试：所有调用都是真实同步方法（由生产代码经 ``asyncio.to_thread`` 放入
线程池），可注入阻塞点、异常和服务端通知。

真实 AMS/TCP 协议路径由 integration 层的 pyads ``AdsTestServer`` 覆盖，
两层不得互相替代。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.units import NONE_UNIT

ADSSTATE_RUN = 5
ADSIGRP_SUMUP_READ = 0xF080
ADSIGRP_SUMUP_WRITE = 0xF081


class Gate:
    """可编排的阻塞点：阻塞直到测试放行，并记录等待线程。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.waiters = 0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            self.waiters += 1
        self._event.wait()

    def open(self) -> None:
        self._event.set()

    @property
    def is_open(self) -> bool:
        return self._event.is_set()


class FakePyadsConnection:
    """记录调用、可注入延迟/异常/通知的 pyads.Connection 替身。"""

    def __init__(self) -> None:
        self.opened = False
        self.closed_calls = 0
        self.thread_ids: list[int] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.timeouts: list[int] = []
        # 可编排阻塞点（键为方法名）。
        self.block_before: dict[str, Gate] = {}
        # 脚本化异常：单轮或持久。
        self.fail: dict[str, BaseException] = {}
        self.fail_once: dict[str, BaseException] = {}
        # Sum Read 应答：{(group, offset): value bytes} 按请求项回填。
        self.sum_read_store: dict[tuple[int, int], bytes] = {}
        self.sum_read_override: Callable[[list[tuple[int, int, int]]], bytes] | None = None
        # Sum Write 逐点错误码（默认全 0）。
        self.sum_write_errors: list[int] | None = None
        self.written: dict[tuple[int, int], bytes] = {}
        # symbol 名 -> (group, offset)。
        self.symbols: dict[str, tuple[int, int]] = {}
        # notification。
        self.notifications: dict[tuple[int, int], tuple[int, int, Callable[..., None]]] = {}
        self.added = 0
        self.removed = 0
        self._next_handle = 1

    def _maybe_block(self, name: str) -> None:
        gate = self.block_before.get(name)
        if gate is not None:
            gate.wait()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_once:
            raise self.fail_once.pop(name)
        if name in self.fail:
            raise self.fail[name]

    def _enter(self, name: str, *args: Any) -> None:
        # 阻塞点先于异常：模拟已进入物理调用但未应答。
        self._maybe_block(name)
        self.calls.append((name, args))
        self.thread_ids.append(threading.get_ident())
        self._maybe_fail(name)

    def open(self) -> None:
        self._enter("open")
        self.opened = True

    def close(self) -> None:
        # pyads 语义：未 open 时 close 静默返回。
        self._maybe_block("close")
        self._maybe_fail("close")
        self.closed_calls += 1
        self.opened = False

    def set_timeout(self, ms: int) -> None:
        self._enter("set_timeout", ms)
        self.timeouts.append(ms)

    def read_state(self) -> tuple[int, int]:
        self._enter("read_state")
        return (ADSSTATE_RUN, 0)

    def get_symbol(self, name: str) -> SimpleNamespace:
        self._enter("get_symbol", name)
        group, offset = self.symbols[name]
        return SimpleNamespace(index_group=group, index_offset=offset)

    def read_write(
        self,
        index_group: int,
        index_offset: int,
        plc_read_datatype: object,
        value: Any,
        plc_write_datatype: object,
        return_ctypes: bool = False,
        check_length: bool = True,
    ) -> bytes:
        self._maybe_block("read_write")
        self.calls.append(("read_write", (index_group, index_offset)))
        self.thread_ids.append(threading.get_ident())
        self._maybe_fail("read_write")
        if index_group == ADSIGRP_SUMUP_READ:
            items = [(item.iGroup, item.iOffset, item.size) for item in value]
            self.calls.append(("read_write_sumread", tuple(items)))
            if self.sum_read_override is not None:
                return self.sum_read_override(items)
            data = b"".join(self.sum_read_store.get((g, o), b"\x00" * s) for g, o, s in items)
            return b"\x00" * (4 * len(items)) + data
        if index_group == ADSIGRP_SUMUP_WRITE:
            count = index_offset
            raw = bytes(value)
            self.calls.append(("read_write_sumwrite", raw))
            headers = 12 * count
            cursor = headers
            # 请求头与本替身无关（生产 codec 有独立单测），这里按 12 字节步进。
            import struct

            for header_index in range(count):
                group, offset, size = struct.unpack_from("<III", raw, header_index * 12)
                self.written[(group, offset)] = raw[cursor : cursor + size]
                cursor += size
            errors = self.sum_write_errors
            if errors is None:
                errors = [0] * count
            return b"".join(struct.pack("<I", code) for code in errors)
        raise AssertionError(f"unexpected index_group 0x{index_group:x}")

    def notification(
        self, datatype: object
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def decorate(func: Callable[..., None]) -> Callable[..., None]:
            return func

        return decorate

    def add_device_notification(
        self,
        data: str | tuple[int, int],
        attr: object,
        callback: Callable[..., None],
        user_handle: int | None = None,
    ) -> tuple[int, int]:
        self._enter("add_device_notification", data)
        assert isinstance(data, tuple)
        handle = self._next_handle
        self._next_handle += 1
        token = (handle, handle + 10_000)
        self.notifications[data] = (token[0], token[1], callback)
        self.added += 1
        return token

    def del_device_notification(self, notification_handle: int, user_handle: int) -> None:
        self._enter("del_device_notification", notification_handle, user_handle)
        for key, item in list(self.notifications.items()):
            if item[0] == notification_handle:
                del self.notifications[key]
        self.removed += 1

    # 测试侧：模拟 PLC 主动通知（在调用线程触发原生回调，等价 pyads router 线程）。
    def fire_notification(
        self,
        data: tuple[int, int],
        payload: bytes,
        *,
        timestamp: Any = None,
    ) -> None:
        from datetime import UTC, datetime

        handle, _, callback = self.notifications[data]
        if timestamp is None:
            timestamp = datetime.now(UTC)
        callback(handle, str(data), timestamp, payload)


def make_point(
    point_id: str,
    *,
    data_type: str = "DINT",
    symbol: str | None = None,
    index_group: int | None = None,
    index_offset: int | None = None,
    size: int | None = None,
) -> Point:
    ext: dict[str, Any] = {"data_type": data_type}
    if symbol is not None:
        ext["symbol"] = symbol
    if index_group is not None:
        ext["index_group"] = index_group
    if index_offset is not None:
        ext["index_offset"] = index_offset
    if size is not None:
        ext["size"] = size
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext=ext,
    )


def make_driver(
    points: dict[str, Point],
    *,
    host: str = "192.0.2.10",
    net_id: str = "192.0.2.10.1.1",
    port: int | None = 851,
    options: dict[str, Any] | None = None,
) -> ADSDriver:
    table = PointTable(PointTableId("ads_pt"), Protocol("ads"), points)
    merged = {"target_net_id": net_id, **(options or {})}
    return ADSDriver(ConnectionEndpoint(host, port), table, merged)


def patch_session(driver: ADSDriver, connection: FakePyadsConnection) -> FakePyadsConnection:
    """让 driver 的下一次 ``_new_session()`` 返回绑定替身的 session。"""
    from core.infrastructure.protocol.ads.session import _AdsSession

    driver._new_session = lambda: _AdsSession(lambda: connection)  # type: ignore[method-assign]
    return connection
