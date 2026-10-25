"""Notification 投递：解码、UTC 时标、队列饱和、回调容错与迟到通知。"""

from __future__ import annotations

import asyncio
import struct
from datetime import UTC, datetime

from core.application.protocol_contract import ProtocolSample, Quality
from tests.support.ads_fake import FakePyadsConnection, make_driver, make_point, patch_session

POINTS = {
    "a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0),
    "b": make_point("b", data_type="INT", index_group=0x4020, index_offset=2),
}


async def _start(connection: FakePyadsConnection):
    driver = make_driver(POINTS)
    patch_session(driver, connection)
    await driver.connect()
    received: list[tuple[ProtocolSample, ...]] = []
    done = asyncio.Event()

    async def callback(batch: tuple[ProtocolSample, ...]) -> None:
        received.append(batch)
        done.set()

    sub = driver.subscribe(("a", "b"), callback, interval=0.01)
    await sub.start()
    return driver, sub, received, done


async def test_notification_delivers_decoded_fields_in_order() -> None:
    connection = FakePyadsConnection()
    driver, sub, received, done = await _start(connection)
    stamp = datetime(2026, 1, 2, 3, 4, 5)  # naive：PLC 上送的 UTC
    connection.fire_notification((0x4020, 0), struct.pack("<hh", -7, 42), timestamp=stamp)
    await asyncio.wait_for(done.wait(), timeout=2)
    batch = received[0]
    assert [sample.point_id for sample in batch] == ["a", "b"]
    assert [sample.value for sample in batch] == [-7, 42]
    assert all(sample.quality is Quality.GOOD for sample in batch)
    assert batch[0].timestamp == stamp.replace(tzinfo=UTC)
    assert batch[0].timestamp.tzinfo is not None
    await sub.stop()
    await driver.close()


async def test_non_datetime_timestamp_uses_local_source() -> None:
    connection = FakePyadsConnection()
    driver, sub, received, done = await _start(connection)
    connection.fire_notification((0x4020, 0), struct.pack("<hh", 1, 2), timestamp=123456)
    await asyncio.wait_for(done.wait(), timeout=2)
    sample = received[0][0]
    assert sample.timestamp_source == "local"
    assert sample.timestamp.tzinfo is not None
    await sub.stop()
    await driver.close()


async def test_bad_payload_size_marks_whole_block_bad() -> None:
    connection = FakePyadsConnection()
    driver, sub, received, done = await _start(connection)
    connection.fire_notification((0x4020, 0), b"\x00")  # 期望 4 字节
    await asyncio.wait_for(done.wait(), timeout=2)
    assert all(sample.quality is Quality.BAD for sample in received[0])
    await sub.stop()
    await driver.close()


async def test_queue_saturation_drops_oldest_without_stalling_native_thread() -> None:
    connection = FakePyadsConnection()
    driver = make_driver(POINTS)
    patch_session(driver, connection)
    await driver.connect()
    received: list[int] = []
    release = asyncio.Event()

    async def slow(batch: tuple[ProtocolSample, ...]) -> None:
        await release.wait()
        received.append(int(batch[0].value))

    sub = driver.subscribe(("a", "b"), slow, interval=0.001)
    await sub.start()
    # 队列上限 1000：在回调被阻塞期间灌入 1005 批。
    for value in range(1005):
        connection.fire_notification((0x4020, 0), struct.pack("<hh", value, 0))
    release.set()
    deadline = asyncio.get_running_loop().time() + 3
    while len(received) < 1000:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    # 最旧的 5 批被丢弃，最新值保留。
    assert received[0] == 5
    assert received[-1] == 1004
    await sub.stop()
    await driver.close()


async def test_callback_exception_is_logged_and_worker_survives() -> None:
    connection = FakePyadsConnection()
    driver = make_driver(POINTS)
    patch_session(driver, connection)
    await driver.connect()
    seen: list[int] = []
    calls = 0

    async def flaky(batch: tuple[ProtocolSample, ...]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("consumer exploded")
        seen.append(int(batch[0].value))

    sub = driver.subscribe(("a", "b"), flaky, interval=0.001)
    await sub.start()
    connection.fire_notification((0x4020, 0), struct.pack("<hh", 1, 0))
    connection.fire_notification((0x4020, 0), struct.pack("<hh", 2, 0))
    deadline = asyncio.get_running_loop().time() + 3
    while not seen:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert seen == [2]  # 第二批仍被投递，worker 未死
    await sub.stop()
    await driver.close()


async def test_late_native_callback_after_stop_is_ignored() -> None:
    connection = FakePyadsConnection()
    driver, sub, received, _ = await _start(connection)
    _, _, native_callback = connection.notifications[(0x4020, 0)]
    await sub.stop()
    # 真实 pyads 在 del 后仍可能有一两个在途回调：不得再入队。
    native_callback(1, "(4020, 0)", datetime.now(UTC), struct.pack("<hh", 9, 9))
    await asyncio.sleep(0.05)
    assert received == []
    await driver.close()
