"""ADS 编解码重复执行的一致性。"""

from __future__ import annotations

import os
import struct

from core.infrastructure.protocol.ads.mapping import ADSPoint


def test_ads_compiled_decoder_repeated_reads() -> None:
    point = ADSPoint(
        point_id="p",
        index_group=0x4020,
        index_offset=24,
        data_type="DINT",
        size=4,
        symbol="MAIN.p",
    )
    raw = struct.pack("<i", -100000)
    rounds = int(os.environ.get("PROTOCOL_STABILITY_ROUNDS", "1000"))
    assert rounds > 0
    for _ in range(rounds):
        assert point.decoder(raw, 0) == -100000


def test_ads_notification_planner_soak() -> None:
    """反复重新构造订阅分块，不得产生错位或数据漂移。"""
    import time

    from core.infrastructure.protocol.ads.codec import compile_notification_blocks

    points = tuple(
        ADSPoint(
            point_id=f"p{i}", index_group=0x4020, index_offset=4 * i,
            data_type="DINT", size=4, symbol=f"MAIN.p{i}"
        )
        for i in range(80)
    )
    duration = float(os.environ.get("PROTOCOL_STABILITY_SECONDS", "5"))
    assert duration > 0
    end = time.monotonic() + duration
    rounds = 0
    while time.monotonic() < end:
        blocks = compile_notification_blocks(points, max_bytes=64)
        assert sum(len(block.fields) for block in blocks) == 80
        assert all(block.size <= 64 for block in blocks)
        rounds += 1
    assert rounds > 0
