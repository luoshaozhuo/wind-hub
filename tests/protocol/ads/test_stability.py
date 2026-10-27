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
