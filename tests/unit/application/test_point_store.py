"""Latest/Trend 内存 Store 单元测试。"""

from datetime import UTC, datetime, timedelta

from wind_hub.domain.model.point import PointValue
from wind_hub.infra.point_store import InMemoryLatestPointStore, InMemoryTrendStore


def _value(point_id: str, value: float, seconds: int = 0) -> PointValue:
    return PointValue(
        device_id="d1",
        point_id=point_id,
        value=value,
        timestamp=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def test_latest_store_overwrites_by_device_and_point() -> None:
    store = InMemoryLatestPointStore()
    store.put_batch([_value("p1", 1.0), _value("p1", 2.0)])

    assert store.get("d1", "p1").value == 2.0


def test_trend_store_is_bounded_and_ordered() -> None:
    store = InMemoryTrendStore(max_samples_per_point=2)
    store.append_batch([_value("p1", 1.0), _value("p1", 2.0), _value("p1", 3.0)])

    rows = store.query("d1", {"p1"}, limit_per_point=10)

    assert [row.value for row in rows["p1"]] == [2.0, 3.0]
