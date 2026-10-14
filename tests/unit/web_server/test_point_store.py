"""短期 Trend 内存 Store 单元测试。"""

import threading
from datetime import UTC, datetime, timedelta

from wind_hub_core.model.point import PointValue
from wind_hub_server.infra.point_store import InMemoryTrendStore


def _value(point_id: str, value: float, seconds: int = 0) -> PointValue:
    return PointValue(
        device_id="d1",
        point_id=point_id,
        value=value,
        timestamp=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def test_trend_store_is_bounded_and_ordered() -> None:
    store = InMemoryTrendStore(max_samples_per_point=2)
    store.append_batch([_value("p1", 1.0), _value("p1", 2.0), _value("p1", 3.0)])

    rows = store.query("d1", {"p1"}, limit_per_point=10)

    assert [row.value for row in rows["p1"]] == [2.0, 3.0]


def test_query_filters_since_and_applies_limit_per_point() -> None:
    store = InMemoryTrendStore()
    store.append_batch([_value("p1", float(i), seconds=i) for i in range(5)])

    since = datetime.now(UTC) + timedelta(seconds=2)
    rows = store.query("d1", {"p1"}, since=since, limit_per_point=2)

    # since 保留 2/3/4 秒三条，limit 取最近两条且保持到达顺序
    assert [row.value for row in rows["p1"]] == [3.0, 4.0]


def test_query_multiple_points_and_missing_point() -> None:
    store = InMemoryTrendStore()
    store.append_batch([_value("p1", 1.0), _value("p2", 2.0)])

    rows = store.query("d1", {"p1", "p2", "p3"})

    assert set(rows) == {"p1", "p2", "p3"}
    assert [row.value for row in rows["p1"]] == [1.0]
    assert [row.value for row in rows["p2"]] == [2.0]
    assert rows["p3"] == []


def test_query_result_list_is_independent_container() -> None:
    store = InMemoryTrendStore()
    store.append_batch([_value("p1", 1.0), _value("p1", 2.0)])

    rows = store.query("d1", {"p1"})
    rows["p1"].clear()
    rows["p1"].append(_value("p1", 99.0))

    again = store.query("d1", {"p1"})
    assert [row.value for row in again["p1"]] == [1.0, 2.0]


def test_elements_are_shared_immutable_references() -> None:
    """不再深拷贝：查询返回同一不可变 PointValue 引用。"""
    store = InMemoryTrendStore()
    value = _value("p1", 1.0)
    store.append_batch([value])

    rows = store.query("d1", {"p1"})

    assert rows["p1"][0] is value


def test_append_and_query_concurrently() -> None:
    """并发 append/query：查询结果始终是某一时刻的一致快照。"""
    store = InMemoryTrendStore(max_samples_per_point=1000)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for i in range(400):
                store.append_batch([_value("p1", float(i), seconds=i % 60)])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(200):
                rows = store.query("d1", {"p1"}, limit_per_point=50)
                values = [row.value for row in rows["p1"]]
                assert len(values) <= 50
                assert values == sorted(values)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    # 单 writer 保证追加值单调递增，任一时刻快照的值序列必然有序
    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    final = store.query("d1", {"p1"}, limit_per_point=1000)
    assert len(final["p1"]) == 400
