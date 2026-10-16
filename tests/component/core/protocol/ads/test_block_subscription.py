"""连续 ADS 通知区块与标量编译解码测试。"""

from __future__ import annotations

import asyncio
import struct
from datetime import UTC, datetime

import pytest

from core.application.errors import ConfigError
from core.application.protocol_contract import ProtocolSample, Quality
from core.infrastructure.protocol.ads.codec import compile_notification_blocks
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.ads.subscription import ADSSubscription


def point(name: str, offset: int, kind: str = "REAL", size: int = 4) -> ADSPoint:
    return ADSPoint(name, 0x4020, offset, kind, size)


def test_block_compiler_rejects_overlap() -> None:
    blocks = compile_notification_blocks((
        point("c", 108), point("a", 100), point("b", 104), point("gap", 200),
    ))
    assert len(blocks) == 1
    assert blocks[0].size == 104
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks((point("a", 100), point("duplicate", 100)))
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks((point("a", 100, "STRING", 20), point("b", 104)))


@pytest.mark.asyncio
async def test_block_callback_delivers_two_samples_with_original_timestamp() -> None:
    collected: list[ProtocolSample] = []
    finished = asyncio.Event()

    async def callback(sample: ProtocolSample) -> None:
        collected.append(sample)
        if len(collected) == 2:
            finished.set()

    subscription = ADSSubscription(
        object(), (point("a", 100), point("b", 104)), callback,
        cycle_time=0.1, loop=asyncio.get_running_loop(),
    )
    block = compile_notification_blocks(subscription._points)[0]
    subscription._worker_task = asyncio.create_task(subscription._worker())
    stamp = datetime.now(UTC)
    try:
        subscription._block_callback(block, 0)(None, None, stamp, struct.pack("<ff", 1.25, 2.5))
        await asyncio.wait_for(finished.wait(), 1)
        assert [sample.value for sample in collected] == [1.25, 2.5]
        assert all(sample.timestamp == stamp for sample in collected)
        assert all(sample.quality == Quality.GOOD for sample in collected)
    finally:
        subscription._closed = True
        subscription._worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await subscription._worker_task


def test_compiled_mixed_type_decoders() -> None:
    blocks = compile_notification_blocks((point("a", 0, "DINT"), point("b", 4, "REAL")))
    fields = blocks[0].fields
    raw = memoryview(struct.pack("<if", -7, 2.5))
    assert [decode(raw, offset) for pt, offset, decode in fields] == [-7, 2.5]

def test_block_compiler_uses_total_span_not_gap() -> None:
    points = (point("a", 100), point("b", 112), point("c", 140))
    blocks = compile_notification_blocks(points, max_bytes=44)
    assert [(block.offset, block.size, len(block.fields)) for block in blocks] == [
        (100, 44, 3),
    ]
    assert [offset for _, offset, _ in blocks[0].fields] == [0, 12, 40]


def test_block_compiler_respects_capacity() -> None:
    points = (point("a", 100), point("b", 108))
    assert len(compile_notification_blocks(points, max_bytes=11)) == 2
    assert len(compile_notification_blocks(points, max_bytes=12)) == 1


def test_ads_block_config_validation() -> None:
    from core.application.errors import ConfigError
    from core.domain import ConnectionEndpoint
    from core.infrastructure.protocol.ads.config import parse_ads_config

    endpoint = ConnectionEndpoint("192.0.2.20", 851)
    config = parse_ads_config(endpoint, {"max_block_bytes": 2048})
    assert config.max_block_bytes == 2048
    with pytest.raises(ConfigError, match="max_block_bytes"):
        parse_ads_config(endpoint, {"max_block_bytes": 0})
    with pytest.raises(ConfigError, match="max_gap_bytes"):
        parse_ads_config(endpoint, {"max_gap_bytes": 16})


@pytest.mark.asyncio
async def test_block_registration_failure_does_not_fallback() -> None:
    """区块注册失败直接传播异常，已注册的 Handle 必须清理。"""
    class Connection:
        def __init__(self) -> None:
            self.registered: list[tuple[int, int]] = []
            self.deleted: list[object] = []

        def notification(self, _datatype: object):
            return lambda callback: callback

        def add_device_notification(self, address, _attr, _callback):
            self.registered.append(address)
            if address == (0x4020, 100):
                raise RuntimeError("block rejected")
            return (len(self.registered), None)

        def del_device_notification(self, handle, _user_handle):
            self.deleted.append(handle)

    connection = Connection()

    async def callback(sample: ProtocolSample) -> None:
        del sample

    subscription = ADSSubscription(
        connection,
        (point("a", 0), point("b", 100), point("c", 104)),
        callback,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
        max_block_bytes=64,
    )
    with pytest.raises(RuntimeError, match="block rejected"):
        await subscription.start()
    assert connection.registered == [(0x4020, 0), (0x4020, 100)]
    assert connection.deleted == [1]
    assert subscription._worker_task is None

@pytest.mark.asyncio
async def test_single_point_registration_uses_raw_block(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    import core.infrastructure.protocol.ads.subscription as mod

    class Attrib:
        def __init__(self, length: int, **kwargs: object) -> None:
            self.length = length

    class Connection:
        def __init__(self) -> None:
            self.datatype: object = None
            self.callback: object = None

        def notification(self, datatype: object):
            self.datatype = datatype
            return lambda callback: callback

        def add_device_notification(self, address, attr, callback):
            self.callback = callback
            assert address == (0x4020, 100)
            assert attr.length == 4
            return (1, None)

        def del_device_notification(self, handle, user_handle):
            pass

    monkeypatch.setattr(mod.pyads, "NotificationAttrib", Attrib)
    connection = Connection()
    samples: list[ProtocolSample] = []
    event = asyncio.Event()

    async def on_sample(sample: ProtocolSample) -> None:
        samples.append(sample)
        event.set()

    subscription = ADSSubscription(
        connection, (point("single", 100),), on_sample,
        cycle_time=0.1, loop=asyncio.get_running_loop(),
    )
    await subscription.start()
    try:
        assert ctypes.sizeof(connection.datatype) == 4
        assert connection.callback is not None
        connection.callback(None, None, datetime.now(UTC), struct.pack("<f", 3.5))
        await asyncio.wait_for(event.wait(), 1)
        assert samples[0].value == 3.5
    finally:
        await subscription.close()
