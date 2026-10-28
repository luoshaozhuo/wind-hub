"""PR#92 Round5 D3：Modbus 跨 Task 寄存器隔离。

不建立网络连接：直接构造 ResolvedSink（task_sources + task_refs）与
ModbusSink，通过 write_task/write 验证**实际 Modbus 寄存器地址**的更新边界。

点表布局（slave 1，holding，uint16，单寄存器）：

    source      ref            地址    归属 Task
    d0.p0       a.shared       100     A（与 B 同源不同寄存器）
    d0.p0       b.shared       300     B
    d1.p1       d1.p1（缺省）  101     A 独有
    d2.p2       d2.p2（缺省）  301     B 独有
"""

from __future__ import annotations

import pytest

from core.application import Quality
from core.application.sink_contract import (
    ModbusSinkAddress,
    ResolvedSink,
    ResolvedSinkPoint,
    SinkSource,
)
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import ModbusSinkConnection
from core.infrastructure.sink.modbus.sink import ModbusSink

pytestmark = pytest.mark.unit

CONNECTION = ModbusSinkConnection(host="127.0.0.1", port=15020).model_dump(mode="json")


def _point(source: tuple[str, str], ref: str, address: int) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id=source[0], point_id=source[1]),
        ref=ref,
        source_data_type="uint16",
        source_unit="none",
        datatype="uint16",
        unit="none",
        address=ModbusSinkAddress(
            slave_id=1,
            register_type="holding",
            address=address,
        ),
    )


POINTS = (
    _point(("d0", "p0"), "a.shared", 100),
    _point(("d0", "p0"), "b.shared", 300),
    _point(("d1", "p1"), "d1.p1", 101),
    _point(("d2", "p2"), "d2.p2", 301),
)

TASK_SOURCES = {
    "A": (("d0", "p0"), ("d1", "p1")),
    "B": (("d0", "p0"), ("d2", "p2")),
}

TASK_REFS = {
    "A": ("a.shared", "d1.p1"),
    "B": ("b.shared", "d2.p2"),
}


def _sink(
    points: tuple[ResolvedSinkPoint, ...] = POINTS,
    task_sources: dict[str, tuple[tuple[str, str], ...]] | None = None,
    task_refs: dict[str, tuple[str, ...]] | None = None,
) -> ModbusSink:
    config = ResolvedSink(
        sink_id="mb",
        kind="modbus",
        enabled=True,
        connection=CONNECTION,
        points=tuple(point.model_dump(mode="json") for point in points),
        task_sources=task_sources if task_sources is not None else TASK_SOURCES,
        task_refs=task_refs if task_refs is not None else TASK_REFS,
    )
    return ModbusSink(config)


def _pv(
    device: str,
    point: str,
    value: float | int | bool | str | None,
    quality: Quality = Quality.GOOD,
) -> PointValue:
    return PointValue(device_id=device, point_id=point, value=value, quality=quality)


def _read(sink: ModbusSink, address: int) -> int:
    return sink.data_path.store.read_registers(1, "holding", address, 1)[0]


# --- 场景 1/2/4：只运行 Task A，且批次夹带全部来源 -----------------------------


async def test_task_a_writes_only_own_registers_even_with_foreign_batch() -> None:
    sink = _sink()
    await sink.write_task(
        "A",
        [_pv("d0", "p0", 10), _pv("d1", "p1", 11), _pv("d2", "p2", 99)],
    )
    assert _read(sink, 100) == 10  # A 的共享源寄存器
    assert _read(sink, 101) == 11  # A 独有
    assert _read(sink, 300) == 0  # B 的共享源寄存器：同源也不得越权
    assert _read(sink, 301) == 0  # B 独有，批次夹带 d2.p2=99 也不得写入


# --- 场景 5：只运行 Task B ----------------------------------------------------


async def test_task_b_writes_only_own_registers() -> None:
    sink = _sink()
    await sink.write_task(
        "B",
        [_pv("d0", "p0", 20), _pv("d2", "p2", 21), _pv("d1", "p1", 88)],
    )
    assert _read(sink, 300) == 20
    assert _read(sink, 301) == 21
    assert _read(sink, 100) == 0
    assert _read(sink, 101) == 0


# --- 场景 3/6：两 Task 交替/同时运行，同源值分别进入不同地址 --------------------


async def test_shared_source_routes_to_distinct_task_registers() -> None:
    sink = _sink()
    await sink.write_task("A", [_pv("d0", "p0", 10)])
    await sink.write_task("B", [_pv("d0", "p0", 20)])
    assert _read(sink, 100) == 10
    assert _read(sink, 300) == 20

    # 再次更新：A 的新值不得覆盖 B 的寄存器，反之亦然
    await sink.write_task("A", [_pv("d0", "p0", 12)])
    assert _read(sink, 100) == 12
    assert _read(sink, 300) == 20
    await sink.write_task("B", [_pv("d0", "p0", 22)])
    assert _read(sink, 300) == 22
    assert _read(sink, 100) == 12


async def test_both_tasks_concurrent_batch_roundtrip() -> None:
    sink = _sink()
    await sink.write_task("A", [_pv("d0", "p0", 1), _pv("d1", "p1", 2)])
    await sink.write_task("B", [_pv("d0", "p0", 3), _pv("d2", "p2", 4)])
    assert (_read(sink, 100), _read(sink, 101), _read(sink, 300), _read(sink, 301)) == (
        1,
        2,
        3,
        4,
    )


# --- 场景 7：停止其中一个 Task（停止投递）不影响另一个 --------------------------


async def test_stopped_task_silence_does_not_affect_other_task() -> None:
    sink = _sink()
    await sink.write_task("A", [_pv("d0", "p0", 10), _pv("d1", "p1", 11)])
    await sink.write_task("B", [_pv("d0", "p0", 20), _pv("d2", "p2", 21)])
    # Task B 停止：只有 A 继续收到新批次
    for tick in range(3):
        await sink.write_task("A", [_pv("d0", "p0", 100 + tick), _pv("d1", "p1", 200 + tick)])
    assert _read(sink, 100) == 102
    assert _read(sink, 101) == 202
    assert _read(sink, 300) == 20  # B 冻结在最后一次真实值
    assert _read(sink, 301) == 21


# --- 未登记 Task 与空批次：fail-closed，无副作用 -------------------------------


async def test_unknown_task_and_empty_batch_write_nothing() -> None:
    sink = _sink()
    await sink.write_task("ghost", [_pv("d0", "p0", 1), _pv("d1", "p1", 1)])
    await sink.write_task("A", [])
    assert _read(sink, 100) == 0
    assert _read(sink, 101) == 0
    assert _read(sink, 300) == 0
    assert _read(sink, 301) == 0


# --- 质量过滤在 Task 路径仍然生效（BAD 不覆盖 GOOD）---------------------------


async def test_task_path_keeps_quality_filtering() -> None:
    sink = _sink()
    await sink.write_task("A", [_pv("d1", "p1", 11)])
    await sink.write_task("A", [_pv("d1", "p1", 0, quality=Quality.BAD)])
    assert _read(sink, 101) == 11  # BAD 不覆盖最近 GOOD 值


# --- 场景 10：普通 write() 是全量路径，契约不同于 write_task() ------------------


async def test_plain_write_uses_full_point_table() -> None:
    """write() 不经 Task 过滤：同一 source 的全部映射寄存器都会更新。

    Runtime 采集流只走 write_task；write() 保留给非 Task 投递与诊断契约。
    """
    sink = _sink()
    await sink.write([_pv("d0", "p0", 7), _pv("d1", "p1", 8), _pv("d2", "p2", 9)])
    assert _read(sink, 100) == 7
    assert _read(sink, 300) == 7  # 同源的两条映射都更新
    assert _read(sink, 101) == 8
    assert _read(sink, 301) == 9


# --- 场景 8：热更新重建后旧映射不再产生写入 ------------------------------------


async def test_rebuilt_sink_does_not_write_legacy_mapping() -> None:
    """Runtime 对 Modbus 采用 close-first 热更新：旧 sink 实例关闭，队列只投递给
    新 sink。这里在新实例上验证旧地址不再可达、新地址接收写入。"""
    old = _sink()
    await old.write_task("A", [_pv("d0", "p0", 10), _pv("d1", "p1", 11)])
    assert _read(old, 100) == 10

    # 新配置：A 的 d0.p0 改映射到地址 120，并移除 d1.p1
    new_points = (
        _point(("d0", "p0"), "a.shared.v2", 120),
        _point(("d0", "p0"), "b.shared", 300),
        _point(("d2", "p2"), "d2.p2", 301),
    )
    new = _sink(
        points=new_points,
        task_sources={"A": (("d0", "p0"),), "B": (("d0", "p0"), ("d2", "p2"))},
        task_refs={"A": ("a.shared.v2",), "B": ("b.shared", "d2.p2")},
    )
    await new.write_task("A", [_pv("d0", "p0", 42), _pv("d1", "p1", 43)])
    assert _read(new, 120) == 42
    assert _read(new, 300) == 0
    assert _read(new, 301) == 0
    # 新数据存储没有旧地址布局，读取旧地址直接 KeyError（旧映射物理消失）
    with pytest.raises(KeyError):
        new.data_path.store.read_registers(1, "holding", 100, 1)
    with pytest.raises(KeyError):
        new.data_path.store.read_registers(1, "holding", 101, 1)
