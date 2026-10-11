"""Collector 最新点值快照测试。"""

from datetime import UTC, datetime, timedelta

from collector.application.latest_values import LatestValues
from core.domain.point_value import PointValue

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _value(device: str, point: str, value: int, *, seconds: float = 0.0) -> PointValue:
    return PointValue(
        device_id=device,
        point_id=point,
        value=value,
        timestamp=_T0 + timedelta(seconds=seconds),
    )


def test_latest_values_overwrite_and_filter() -> None:
    cache = LatestValues()
    cache.update([_value("d1", "p1", 1), _value("d2", "p1", 2)])
    cache.update([_value("d1", "p1", 3, seconds=1)])
    assert [(v.device_id, v.value) for v in cache.snapshot()] == [
        ("d1", 3),
        ("d2", 2),
    ]
    assert [v.value for v in cache.snapshot("d1")] == [3]
    cache.clear()
    assert cache.snapshot() == []


def test_out_of_order_sample_does_not_overwrite_newer() -> None:
    """乱序规则：采样时间更早的迟到上报不得覆盖较新采样。"""
    cache = LatestValues()
    cache.update([_value("d1", "p1", 2, seconds=10)])
    cache.update([_value("d1", "p1", 1, seconds=5)])
    assert [v.value for v in cache.snapshot("d1")] == [2]

    # 相同采样时间的重复派发以最后到达为准。
    cache.update([_value("d1", "p1", 4, seconds=10)])
    assert [v.value for v in cache.snapshot("d1")] == [4]


def test_retain_prunes_removed_devices_and_points() -> None:
    """配置激活后按有效点表裁剪，保留键的时间戳不得被伪造。"""
    cache = LatestValues()
    cache.update(
        [
            _value("d1", "p1", 1, seconds=1),
            _value("d1", "p2", 2, seconds=2),
            _value("d2", "p1", 3, seconds=3),
        ]
    )
    cache.retain({"d1": frozenset({"p1"})})
    remaining = cache.snapshot()
    assert [(v.device_id, v.point_id) for v in remaining] == [("d1", "p1")]
    assert remaining[0].timestamp == _T0 + timedelta(seconds=1)


def test_late_callback_after_retain_cannot_reinsert_removed_point() -> None:
    """配置激活后，旧订阅迟到回调携带的已删除测点不得重新进入缓存。"""
    from datetime import UTC, datetime

    cache = LatestValues()
    cache.retain({"dev1": frozenset({"p1"})})
    cache.update(
        [
            PointValue(
                device_id="dev1",
                point_id="p2",
                value=2.0,
                timestamp=datetime.now(UTC),
            )
        ]
    )
    assert cache.snapshot() == []

    # 有效键内的测点仍正常更新。
    cache.update(
        [
            PointValue(
                device_id="dev1",
                point_id="p1",
                value=1.0,
                timestamp=datetime.now(UTC),
            )
        ]
    )
    assert [value.point_id for value in cache.snapshot()] == ["p1"]


def test_update_before_first_retain_is_unconstrained() -> None:
    """首次配置激活前（初始配置即基线）不做键约束。"""
    from datetime import UTC, datetime

    cache = LatestValues()
    cache.update(
        [
            PointValue(
                device_id="dev1",
                point_id="p9",
                value=1.0,
                timestamp=datetime.now(UTC),
            )
        ]
    )
    assert [value.point_id for value in cache.snapshot()] == ["p9"]
