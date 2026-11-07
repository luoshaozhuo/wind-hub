"""ST-40 Modbus 分组/缓存参数动态调整后旧计划失效（设计 §14.3 ST-40）。

合同：参数（read_merge_max_gap / read_plan_cache_size）属于装配期协议选项，
调整参数 = 用同一静态点表重新装配 Driver。要求：
1. 旧 Driver 的读取计划不得泄漏到新 Driver（新实例缓存从空开始）；
2. 新规划严格按新参数构建（max_gap=0 不跨空洞合并，max_gap=8 合并）；
3. read_plan_cache_size=0 时完全不缓存；
4. 静态点表对象与点位定义在两次装配间保持不变；
5. 非法参数在装配解析期被明确拒绝，不静默回退默认值。
"""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.domain import ConnectionEndpoint, Point, PointAccess, PointTable, Protocol
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.system, pytest.mark.reload, pytest.mark.modbus]


def _point(point_id: str, address: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ,
        ext={
            "address": address,
            "register_type": "holding",
            "data_type": "uint16",
            "slave_id": 1,
        },
    )


class _RecordingSession:
    """记录原生读取区间的假会话；不触网，区间形状即规划证据。"""

    def __init__(self) -> None:
        self.reads: list[tuple[str, int, int, int]] = []

    def is_open(self) -> bool:
        return True

    async def read(self, register_type: str, address: int, count: int, slave_id: int) -> list[int]:
        self.reads.append((register_type, address, count, slave_id))
        return [0] * count


def _driver(table: PointTable, options: dict[str, int]) -> ModbusDriver:
    return ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), table, options)


async def test_st_40_param_change_rebuilds_plans_while_static_table_unchanged() -> None:
    # p0@0 与 p1@3 之间隔两个空寄存器地址（1、2），空洞跨度=2。
    static_points = {"p0": _point("p0", 0), "p1": _point("p1", 3)}
    table = PointTable(PointTableId("st40"), Protocol("modbus"), static_points)

    # 第一次装配：max_gap=0 —— 任何空洞都不允许跨并，必须两个独立请求。
    driver_a = _driver(table, {"read_merge_max_gap": 0, "read_plan_cache_size": 32})
    session_a = _RecordingSession()
    driver_a._session = session_a
    await driver_a.read_many(["p0", "p1"])
    assert [(addr, count) for _, addr, count, _ in session_a.reads] == [(0, 1), (3, 1)]
    cached_key = ("p0", "p1")
    plan_a = driver_a._read_plan_cache[cached_key]
    assert len(plan_a) == 2

    # 重新装配：同一点表对象、max_gap=8 —— 空洞允许跨越，合并为单个区间请求。
    assert driver_a._point_table is table
    driver_b = _driver(table, {"read_merge_max_gap": 8, "read_plan_cache_size": 32})
    # 新 Driver 的计划缓存必须从空开始，旧计划不随参数调整泄漏。
    assert driver_b._read_plan_cache == {}
    session_b = _RecordingSession()
    driver_b._session = session_b
    await driver_b.read_many(["p0", "p1"])
    assert [(addr, count) for _, addr, count, _ in session_b.reads] == [(0, 4)]
    plan_b = driver_b._read_plan_cache[cached_key]
    assert len(plan_b) == 1
    # 静态点表不变：两个 Driver 编译出的点位身份与地址完全一致。
    assert driver_b._point_table is table
    assert set(driver_a._points) == set(driver_b._points) == {"p0", "p1"}
    assert driver_a._points["p0"].address == driver_b._points["p0"].address == 0
    assert driver_a._points["p1"].address == driver_b._points["p1"].address == 3


async def test_st_40_cache_disabled_never_retains_plans() -> None:
    table = PointTable(
        PointTableId("st40-no-cache"),
        Protocol("modbus"),
        {"p0": _point("p0", 10), "p1": _point("p1", 11)},
    )
    driver = _driver(table, {"read_merge_max_gap": 8, "read_plan_cache_size": 0})
    driver._session = _RecordingSession()
    await driver.read_many(["p0", "p1"])
    await driver.read_many(["p1", "p0"])
    # 容量为 0：每次调用都重新规划，缓存恒为空。
    assert driver._read_plan_cache == {}


async def test_st_40_repeated_read_reuses_cached_plan_with_unchanged_params() -> None:
    table = PointTable(
        PointTableId("st40-reuse"),
        Protocol("modbus"),
        {"p0": _point("p0", 20), "p1": _point("p1", 21)},
    )
    driver = _driver(table, {"read_merge_max_gap": 8, "read_plan_cache_size": 32})
    driver._session = _RecordingSession()
    await driver.read_many(["p0", "p1"])
    plan = driver._read_plan_cache[("p0", "p1")]
    await driver.read_many(["p0", "p1"])
    # 参数未变：同一计划对象被复用，而非重新构建。
    assert driver._read_plan_cache[("p0", "p1")] is plan


@pytest.mark.parametrize(
    "bad_options",
    [
        {"read_merge_max_gap": -1},
        {"read_plan_cache_size": -1},
    ],
)
def test_st_40_invalid_params_rejected_at_assembly(bad_options: dict[str, int]) -> None:
    table = PointTable(PointTableId("st40-bad"), Protocol("modbus"), {"p0": _point("p0", 0)})
    with pytest.raises(ConfigError, match=">= 0"):
        _driver(table, bad_options)
