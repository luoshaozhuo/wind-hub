"""使用真实 pyads 客户端协议对象验证 ADS 解码布局。"""

from __future__ import annotations

import pyads

from core.infrastructure.protocol.ads.codec import encode_sum_write
from core.infrastructure.protocol.ads.mapping import ADSPoint


def test_sum_write_uses_ads_native_address_shape() -> None:
    point = ADSPoint(
        point_id="p",
        index_group=pyads.ADSIGRP_SYM_VALBYHND,
        index_offset=20,
        data_type="INT",
        size=2,
        symbol="MAIN.p",
    )
    payload = encode_sum_write([(0, point, b"\x01\x00")])
    assert len(payload) == 14
    assert int.from_bytes(payload[0:4], "little") == pyads.ADSIGRP_SYM_VALBYHND
