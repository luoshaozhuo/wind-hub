"""IEC104 组件测试用的内存消息级假连接。

新 clean rewrite 的 Session 直接调用 ``hat.drivers.iec104.connect``，不再有
``_dial`` 字节 seam。本假连接在 **hat 消息层**（非 APCI 字节层）替换传输：
- 上行：测试通过 :meth:`FakeConnection.deliver` 投递 hat 报文列表，进入驱动
  唯一的 ``receive()`` 循环；
- 下行：:meth:`FakeConnection.send` 捕获驱动发出的 hat 报文。

APCI/真实字节形态由 integration 测试（真实 c104 从站/黑洞）覆盖，这里专注
关联、生命周期与状态机的确定性验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import common as iec104common

from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
)
from core.domain import (
    Protocol as DeviceProtocol,
)
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.support.units import NONE_UNIT


class FakeConnection:
    """``hat.drivers.iec104.Connection`` 的内存替身。"""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[list[iec101.Msg]] = asyncio.Queue()
        self.sent_messages: list[iec101.Msg] = []
        self.is_enabled = False
        self.enabled_cbs: list[Any] = []
        self.closed = False
        self.close_error: Exception | None = None

    async def send(self, messages: Iterable[iec101.Msg]) -> None:
        if self.closed:
            raise ConnectionError("fake connection closed")
        for msg in messages:
            self.sent_messages.append(msg)

    async def receive(self) -> list[iec101.Msg]:
        messages = await self._inbound.get()
        if self.closed:
            raise ConnectionError("fake connection closed")
        return messages

    async def async_close(self) -> None:
        if self.close_error is not None:
            # 模拟物理关闭失败：不标记 closed，唤醒接收循环抛错。
            self.closed = True
            self.is_enabled = False
            self._inbound.put_nowait([])
            raise self.close_error
        if self.closed:
            return
        self.closed = True
        self.is_enabled = False
        self._inbound.put_nowait([])

    def register_enabled_cb(self, callback: Any) -> object:
        self.enabled_cbs.append(callback)
        loop = asyncio.get_running_loop()
        # STARTDT 在 connect 返回后由协议栈异步确认。
        loop.call_soon(self._enable)
        return object()

    def _enable(self) -> None:
        self.is_enabled = True
        for callback in self.enabled_cbs:
            callback(True)

    def notify_enabled(self, enabled: bool) -> None:
        self.is_enabled = enabled
        for callback in self.enabled_cbs:
            callback(enabled)

    # -- 测试驱动 -------------------------------------------------------

    def deliver(self, messages: list[iec101.Msg]) -> None:
        """把一批 hat 报文投递进唯一接收循环。"""
        self._inbound.put_nowait(messages)

    def last_message(self) -> iec101.Msg | None:
        return self.sent_messages[-1] if self.sent_messages else None


def install_fake_connect(
    monkeypatch: Any, connection: FakeConnection | None = None
) -> FakeConnection:
    """把 Session 使用的 ``iec104.connect`` 替换为返回内存假连接。"""
    conn = connection or FakeConnection()

    async def _fake_connect(addr: Any, **kwargs: Any) -> FakeConnection:
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _fake_connect)
    return conn


def make_point(
    point_id: str,
    ioa: int,
    *,
    ca: int = 1,
    type_id: str | None = "M_ME_NC_1",
    access: PointAccess = PointAccess.READ,
    extra: dict[str, Any] | None = None,
) -> Point:
    ext: dict[str, Any] = {"ioa": ioa, "ca": ca}
    if type_id is not None:
        ext["type_id"] = type_id
    if extra:
        ext.update(extra)
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


def make_table(points: list[Point]) -> PointTable:
    return PointTable(
        point_table_id=PointTableId("iec_pt"),
        protocol=DeviceProtocol("iec104"),
        points={point.point_id: point for point in points},
    )


def make_driver(
    points: list[Point],
    *,
    common_addresses: str = "1",
    options: dict[str, Any] | None = None,
) -> IEC104Driver:
    merged: dict[str, Any] = {
        "common_addresses": common_addresses,
        "t1": 1.0,
        "t2": 0.5,
        "t3": 30.0,
        "t0": 2.0,
    }
    if options:
        merged.update(options)
    return IEC104Driver(ConnectionEndpoint("127.0.0.1", 2404), make_table(points), merged)


async def connect_driver(driver: IEC104Driver, monkeypatch: Any) -> FakeConnection:
    conn = install_fake_connect(monkeypatch)
    await driver.connect()
    return conn


# ---------------------------------------------------------------------------
# hat 报文构造快捷方式
# ---------------------------------------------------------------------------


def quality(invalid: bool = False, uncertain: bool = False) -> iec101.MeasurementQuality:
    return iec101.MeasurementQuality(
        invalid=invalid,
        not_topical=uncertain,
        substituted=False,
        blocked=False,
        overflow=False,
    )


def floating_data_msg(
    ca: int,
    ioa: int,
    value: float,
    *,
    cause: Any = iec101.DataResCause.SPONTANEOUS,
    invalid: bool = False,
    time: Any = None,
) -> iec101.DataMsg:
    return iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.FloatingData(iec101.FloatingValue(value), quality(invalid)),
        time=time,
        cause=cause,
    )


def command_msg(
    ca: int,
    ioa: int,
    command: iec101.Command,
    *,
    cause: Any = iec104common.CommandResCause.ACTIVATION_CONFIRMATION,
    negative: bool = False,
) -> iec104common.CommandMsg:
    return iec104common.CommandMsg(
        is_test=False,
        originator_address=0,
        asdu_address=ca,
        io_address=ioa,
        command=command,
        is_negative_confirm=negative,
        time=None,
        cause=cause,
    )


def interrogation_reply(
    ca: int,
    *,
    request: int = 20,
    cause: Any = iec104common.CommandResCause.ACTIVATION_CONFIRMATION,
    negative: bool = False,
) -> iec104common.InterrogationMsg:
    return iec104common.InterrogationMsg(
        is_test=False,
        originator_address=0,
        asdu_address=ca,
        request=request,
        is_negative_confirm=negative,
        cause=cause,
    )


def counter_interrogation_reply(
    ca: int,
    *,
    request: int = 5,
    freeze: Any = None,
    cause: Any = iec104common.CommandResCause.ACTIVATION_CONFIRMATION,
    negative: bool = False,
) -> iec104common.CounterInterrogationMsg:
    return iec104common.CounterInterrogationMsg(
        is_test=False,
        originator_address=0,
        asdu_address=ca,
        request=request,
        freeze=freeze if freeze is not None else iec104common.FreezeCode.READ,
        is_negative_confirm=negative,
        cause=cause,
    )
