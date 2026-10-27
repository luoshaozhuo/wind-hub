"""ADS Notification 分块确定性与偏移数据隔离。"""

from __future__ import annotations

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
