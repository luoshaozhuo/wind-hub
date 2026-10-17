"""IEC104 组件/可靠性测试用的内存 APCI 假连接。

不 mock 编解码：上下行都走 hat 生产 Encoder 的真实字节管线，driver
接收任务看到的字节形态与真链路一致；只替换 TCP/STARTDT 传输层。
"""

from __future__ import annotations

import asyncio
from typing import Any

from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import encoder as iec104_encoder

from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
)
from core.domain import Protocol as DeviceProtocol
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.support.units import NONE_UNIT


def encode_messages(messages: list[iec101.Msg]) -> list[bytes]:
    """把 hat 报文编码为 APDU 载荷字节列表（生产编码器）。"""
    return list(iec104_encoder.Encoder().encode(messages))


def decode_message(data: bytes) -> iec101.Msg:
    """解码一帧上行请求（测试断言用）。"""
    return next(iec104_encoder.Encoder().decode(data))


class FakeApciConnection:
    """hat apci.Connection 的内存替身：双队列字节管道。"""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.is_enabled = True
        self.sent_messages: list[iec101.Msg] = []
        self._enabled_cbs: list[Any] = []
        self._closed = False

    async def send(self, data: bytes, wait_ack: bool = False) -> None:
        del wait_ack
        if self._closed:
            raise ConnectionError("fake connection closed")
        self.sent.append(data)
        self.sent_messages.append(decode_message(data))

    async def receive(self) -> bytes:
        data = await self._inbound.get()
        if self._closed:
            raise ConnectionError("fake connection closed")
        return data

    async def async_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.is_enabled = False
        # 唤醒可能在 receive() 等待的接收任务：用关闭后的哨兵通道。
        await self._inbound.put(b"")

    def register_enabled_cb(self, callback: Any) -> None:
        self._enabled_cbs.append(callback)

    # -- 测试驱动 -------------------------------------------------------

    def deliver(self, messages: list[iec101.Msg]) -> None:
        """把若干报文经真实编码投递到 driver 接收管线。"""
        for chunk in encode_messages(messages):
            self._inbound.put_nowait(chunk)

    def notify_enabled(self, enabled: bool) -> None:
        self.is_enabled = enabled
        for callback in self._enabled_cbs:
            callback(enabled)

    def last_message(self) -> iec101.Msg | None:
        return self.sent_messages[-1] if self.sent_messages else None


def make_point(
    point_id: str,
    ioa: int,
    *,
    ca: int = 1,
    type_id: str | None = "M_ME_NC_1",
    access: PointAccess = PointAccess.READ,
) -> Point:
    ext: dict[str, str | int] = {"ioa": ioa, "ca": ca}
    if type_id is not None:
        ext["type_id"] = type_id
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


def make_driver(
    points: list[Point],
    *,
    common_addresses: str = "1",
    options: dict[str, Any] | None = None,
    health_point: str | None = None,
) -> tuple[IEC104Driver, FakeApciConnection]:
    """构造已 patch 网络 seam 的 Driver + 内存假连接（尚未 connect）。"""
    merged: dict[str, Any] = {
        "common_addresses": common_addresses,
        "t1": 0.5,
        "t2": 0.2,
        "t3": 30.0,
    }
    if health_point is not None:
        merged["health_point"] = health_point
    if options:
        merged.update(options)
    table = PointTable(
        point_table_id=PointTableId("iec_pt"),
        protocol=DeviceProtocol("iec104"),
        points={point.point_id: point for point in points},
    )
    driver = IEC104Driver(ConnectionEndpoint("127.0.0.1", 2404), table, merged)
    conn = FakeApciConnection()
    patch_dial(driver, conn)
    return driver, conn


async def connected_driver(
    points: list[Point],
    **kwargs: Any,
) -> tuple[IEC104Driver, FakeApciConnection]:
    driver, conn = make_driver(points, **kwargs)
    await driver.connect()
    return driver, conn


def patch_dial(driver: Any, conn: FakeApciConnection) -> None:
    """把 Driver 的网络 seam 替换为内存假连接。"""

    async def _fake_dial() -> FakeApciConnection:
        return conn

    driver._dial = _fake_dial


# ---------------------------------------------------------------------------
# 报文构造快捷方式
# ---------------------------------------------------------------------------


def floating_data_msg(
    ca: int,
    ioa: int,
    value: float,
    *,
    cause: Any = iec101.DataResCause.SPONTANEOUS,
    invalid: bool = False,
    time: Any = None,
) -> iec101.DataMsg:
    quality = iec101.MeasurementQuality(
        invalid=invalid,
        not_topical=False,
        substituted=False,
        blocked=False,
        overflow=False,
    )
    return iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.FloatingData(iec101.FloatingValue(value), quality),
        time=time,
        cause=cause,
    )


def read_negative_msg(ca: int, ioa: int, cause: Any) -> iec101.ReadMsg:
    return iec101.ReadMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        cause=cause,
    )


def command_confirmation_msg(
    ca: int,
    ioa: int,
    *,
    cause: Any = iec101.CommandResCause.ACTIVATION_CONFIRMATION,
    negative: bool = False,
) -> iec101.Msg:
    from hat.drivers.iec104 import common as iec104common

    return iec104common.CommandMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        command=iec101.SingleCommand(
            value=iec101.SingleValue.ON,
            select=False,
            qualifier=0,
        ),
        is_negative_confirm=negative,
        time=None,
        cause=cause,
    )


def interrogation_msg(
    ca: int,
    *,
    cause: Any = iec101.CommandResCause.ACTIVATION_CONFIRMATION,
    negative: bool = False,
    qoi: int = 20,
) -> iec101.InterrogationMsg:
    return iec101.InterrogationMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        request=qoi,
        is_negative_confirm=negative,
        cause=cause,
    )
