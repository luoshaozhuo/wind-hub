"""接收管线韧性：解码/分发逐消息隔离 + 畸形数据与坏品质分流。

覆盖：

- 不支持的 ASDU 类型、截断 APDU、多 IO ASDU 中途解码失败：只丢当前
  APDU 剩余部分，接收 worker 不死亡，后续 APDU 正常处理；
- 单条报文分发异常不影响同会话后续报文；
- 主动读响应畸形（协议类型不受支持）→ future 显式 ProtocolError，
  绝不以伪造 BAD 样本完成；其他地址的畸形响应不连累在途读取；
- 自发数据畸形 → 记日志丢弃，绝不伪造样本；后续合法自发照常投递；
- 合法数据携带 Invalid 品质位 → quality=BAD 的真实样本（值保留）。
"""

from __future__ import annotations

import asyncio
import struct

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolSample, Quality
from tests.support.iec104_hat_fake import (
    connected_driver,
    encode_messages,
    floating_data_msg,
    make_point,
)


def _inject(conn, frame: bytes) -> None:
    """绕过假连接的编码入口，直接投递一帧原始 APDU 字节。"""
    conn._inbound.put_nowait(frame)  # type: ignore[attr-defined]


def _io_element(ioa: int, value: float, quality: int = 0) -> bytes:
    return bytes([ioa & 0xFF, (ioa >> 8) & 0xFF, (ioa >> 16) & 0xFF]) + struct.pack(
        "<f", value
    ) + bytes([quality])


# hat 支持但本 Driver 不映射的保护事件 ASDU（M_EP_TA 系），
# 解码后 codec.decode_data 对 ProtectionData 抛 ValueError。
_UNSUPPORTED_DATA_FRAME_TYPE = 38


def _unsupported_data_frame(ioa: int, cause_byte: int) -> bytes:
    return (
        bytes([_UNSUPPORTED_DATA_FRAME_TYPE, 1, cause_byte, 1, 1, 0])
        + bytes([ioa & 0xFF, (ioa >> 8) & 0xFF, (ioa >> 16) & 0xFF])
        + b"\x01"
        + b"\x00" * 12
    )


# ---------------------------------------------------------------------------
# §4 解码与分发隔离
# ---------------------------------------------------------------------------


async def test_unsupported_asdu_type_is_logged_and_receive_continues() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    received: list[str] = []

    async def collect(sample: ProtocolSample) -> None:
        received.append(sample.point_id)

    await driver.listen((), collect)
    # 0x68 启动字节被 hat 当作 ASDU 类型 → AsduTypeError。
    _inject(conn, bytes([0x68, 0x04, 0x01, 0x00, 0x00, 0x00]))
    conn.deliver([floating_data_msg(1, 100, 7.5)])
    await asyncio.sleep(0.1)
    assert received == ["p1"]
    assert driver.is_open()
    await driver.close()


async def test_truncated_apdu_dropped_without_stopping_worker() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    received: list[float] = []

    async def collect(sample: ProtocolSample) -> None:
        received.append(float(sample.value))  # type: ignore[arg-type]

    await driver.listen((), collect)
    chunks = encode_messages([floating_data_msg(1, 100, 1.0)])
    _inject(conn, chunks[0][:-3])  # 截断：hat 迭代时 struct.error
    conn.deliver([floating_data_msg(1, 100, 2.0)])
    await asyncio.sleep(0.1)
    assert received == [2.0]
    await driver.close()


async def test_malformed_multi_io_asdu_dropped_and_receive_continues() -> None:
    driver, conn = await connected_driver(
        [make_point("p1", 100), make_point("p2", 101)]
    )
    received: list[str] = []

    async def collect(sample: ProtocolSample) -> None:
        received.append(sample.point_id)

    await driver.listen((), collect)
    # VSQ=2，第二个 IO 元素截断：hat 在进入生成器时急切解码整个 ASDU，
    # 首个 next() 即抛 struct.error——整条 APDU 丢弃并记日志（允许
    # 丢弃不可解码剩余部分），接收 worker 必须继续处理后续 APDU。
    frame = (
        bytes([13, 2, 3, 1, 1, 0])
        + _io_element(100, 1.0)
        + _io_element(101, 2.0)[:4]
    )
    _inject(conn, frame)
    conn.deliver([floating_data_msg(1, 100, 3.0)])
    await asyncio.sleep(0.1)
    assert received == ["p1"]
    await driver.close()


async def test_dispatch_exception_does_not_stop_receive_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    received: list[float] = []

    async def collect(sample: ProtocolSample) -> None:
        received.append(float(sample.value))  # type: ignore[arg-type]

    await driver.listen((), collect)
    original_dispatch = driver._dispatch
    calls = 0

    def flaky_dispatch(session: object, message: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("dispatch bug simulation")
        original_dispatch(session, message)

    monkeypatch.setattr(driver, "_dispatch", flaky_dispatch)
    conn.deliver([floating_data_msg(1, 100, 1.0)])
    conn.deliver([floating_data_msg(1, 100, 2.0)])
    await asyncio.sleep(0.1)
    assert received == [2.0]
    await driver.close()


# ---------------------------------------------------------------------------
# §5 畸形协议数据 vs 坏品质合法数据
# ---------------------------------------------------------------------------


async def test_malformed_read_response_fails_future_with_protocol_error() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    task = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)
    _inject(conn, _unsupported_data_frame(100, cause_byte=5))  # COT REQUEST
    with pytest.raises(ProtocolError, match="malformed read response"):
        await asyncio.wait_for(task, timeout=1.0)

    # 连接与接收管线仍然可用：后续合法读取成功。
    task2 = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)
    conn.deliver(
        [floating_data_msg(1, 100, 9.0, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(task2, timeout=1.0)
    assert sample.value == pytest.approx(9.0)
    assert driver.is_open()
    await driver.close()


async def test_malformed_response_at_other_address_does_not_fail_pending_read() -> None:
    driver, conn = await connected_driver(
        [make_point("p1", 100), make_point("p2", 101)]
    )
    task = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)
    # p2 地址上的畸形读响应不能以任何方式完成 p1 的 future。
    _inject(conn, _unsupported_data_frame(101, cause_byte=5))
    await asyncio.sleep(0.1)
    assert not task.done()
    conn.deliver(
        [floating_data_msg(1, 100, 4.0, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(task, timeout=1.0)
    assert sample.value == pytest.approx(4.0)
    await driver.close()


async def test_malformed_spontaneous_data_is_dropped_not_fabricated() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    received: list[ProtocolSample] = []

    async def collect(sample: ProtocolSample) -> None:
        received.append(sample)

    await driver.listen((), collect)
    _inject(conn, _unsupported_data_frame(100, cause_byte=3))  # COT SPONTANEOUS
    conn.deliver([floating_data_msg(1, 100, 5.0)])
    await asyncio.sleep(0.1)
    assert len(received) == 1
    assert received[0].value == pytest.approx(5.0)
    await driver.close()


async def test_invalid_quality_data_yields_bad_sample_with_preserved_value() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    task = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)
    conn.deliver(
        [
            floating_data_msg(
                1, 100, 6.25, invalid=True, cause=iec101.DataResCause.REQUEST
            )
        ]
    )
    sample = await asyncio.wait_for(task, timeout=1.0)
    # 合法数据 + Invalid 品质位：值仍可用，质量显式 BAD，不是伪造样本。
    assert sample.value == pytest.approx(6.25)
    assert sample.quality is Quality.BAD
    assert sample.timestamp is not None
    await driver.close()
