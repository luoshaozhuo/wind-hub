"""Collector 最新点值快照测试。"""

from core.domain.point_value import PointValue

from collector.application.latest_values import LatestValues


def test_latest_values_overwrite_and_filter() -> None:
    cache = LatestValues()
    cache.update([
        PointValue(device_id="d1", point_id="p1", value=1),
        PointValue(device_id="d2", point_id="p1", value=2),
    ])
    cache.update([PointValue(device_id="d1", point_id="p1", value=3)])
    assert [(v.device_id, v.value) for v in cache.snapshot()] == [
        ("d1", 3), ("d2", 2),
    ]
    assert [v.value for v in cache.snapshot("d1")] == [3]
    cache.clear_device("d1")
    assert [v.device_id for v in cache.snapshot()] == ["d2"]
    cache.clear()
    assert cache.snapshot() == []
