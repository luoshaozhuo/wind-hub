"""ADS Notification 分块确定性与偏移数据隔离。"""

from __future__ import annotations

import asyncio

import pytest

from core.infrastructure.protocol.ads.codec import compile_notification_blocks
from core.infrastructure.protocol.ads.mapping import ADSPoint


def test_many_ads_points_have_unique_relative_offsets() -> None:
    points = [
        ADSPoint(
            point_id=f"p{index}",
            index_group=0x4020,
            index_offset=index * 4,
            data_type="DINT",
            size=4,
            symbol=f"MAIN.p{index}",
        )
        for index in range(100)
    ]
    blocks = compile_notification_blocks(points, max_bytes=40)
    assert len(blocks) == 10
    assert sum(len(block.fields) for block in blocks) == 100
    for block in blocks:
        assert [offset for _, offset, _ in block.fields] == list(range(0, 40, 4))


@pytest.mark.asyncio
async def test_cancelled_ads_waiter_does_not_release_native_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实线程仍在 PLC 操作时，取消 await 不得使 close 提前释放资源。"""
    import threading

    from core.infrastructure.protocol.ads.session import _AdsSession

    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class FakeConnection:
        is_open = False

        def __init__(self, target: str, port: int, host: str) -> None:
            del target, port, host

        def open(self) -> None:
            self.is_open = True
            calls.append("open")

        def read(self) -> bytes:
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release native read")
            calls.append("read")
            return b"ok"

        def close(self) -> None:
            calls.append("close")
            self.is_open = False

    monkeypatch.setattr(
        "core.infrastructure.protocol.ads.session.pyads.Connection", FakeConnection
    )
    session = _AdsSession("1.2.3.4.5.6", 851, "127.0.0.1")
    await session.open()

    waiter = asyncio.create_task(session.execute(operation="read"))
    assert await asyncio.to_thread(started.wait, 3.0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not closing.done(), "native call still holds the ADS command gate"
    release.set()
    await asyncio.wait_for(closing, 3.0)
    assert session.is_settled
    assert calls == ["open", "read", "close"]
