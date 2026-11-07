"""ST-26 / ST-53 / ST-54 点表在线更新事务合同（设计 §14.3、表 7-6/7-7）。

以真实 ModbusDriver 事务机为被测对象，wire 层使用可控假会话注入
验证读数、验证故障与截止时间；不触碰生产代码。

ST-53：更新结果全字段与全状态（SUCCESS/UNCHANGED/VALIDATION_FAILED/
       VERIFICATION_FAILED/NOT_READY/TIMEOUT）及代次规则。
ST-26：NOT_READY、TIMEOUT 与部分应用（不可读变更点进入 unverified_points）。
ST-54：提交前任何故障（校验/验证/超时）都不得切换映射，旧计划保留，
       active_version 不增代，资源状态如实报告。
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.protocol_contract import PointTableUpdateStatus
from core.domain import ConnectionEndpoint, Point, PointAccess, PointTable, Protocol
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.system, pytest.mark.reliability, pytest.mark.timeout, pytest.mark.modbus]


def _point(
    point_id: str,
    address: int,
    *,
    access: PointAccess = PointAccess.READ,
    register_type: str = "holding",
    data_type: str = "uint16",
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext={
            "address": address,
            "register_type": register_type,
            "data_type": data_type,
            "slave_id": 1,
        },
    )


def _table(table_id: str, points: dict[str, Point]) -> PointTable:
    return PointTable(PointTableId(table_id), Protocol("modbus"), points)


class _OkSession:
    def is_open(self) -> bool:
        return True

    async def read(self, register_type: str, address: int, count: int, slave_id: int) -> list[int]:
        return [0] * count


class _FailingSession:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def is_open(self) -> bool:
        return True

    async def read(self, register_type: str, address: int, count: int, slave_id: int) -> list[int]:
        raise self._exc


class _BlockingSession:
    def __init__(self) -> None:
        self._gate = asyncio.Event()

    def is_open(self) -> bool:
        return True

    async def read(self, register_type: str, address: int, count: int, slave_id: int) -> list[int]:
        await self._gate.wait()
        return []


def _driver(table: PointTable) -> ModbusDriver:
    return ModbusDriver(ConnectionEndpoint("127.0.0.1", 502), table, {})


async def test_st_53_success_result_exposes_full_contract_fields_and_generation() -> None:
    base = _table("t1", {"p0": _point("p0", 0)})
    driver = _driver(base)
    driver._session = _OkSession()
    assert driver._active_version == 1

    updated = _table("t2", {"p0": _point("p0", 0), "p1": _point("p1", 1)})
    result = await driver.update_point_table(updated, timeout=5.0)

    assert result.status is PointTableUpdateStatus.SUCCESS
    assert result.applied is True
    assert result.active_version == 2
    assert result.point_errors == ()
    assert result.resource_state == "READY"
    assert result.message  # 非空人类可读消息
    assert driver._point_table is updated
    assert set(driver._points) == {"p0", "p1"}

    # 同表重复提交：UNCHANGED 不增代。
    unchanged = await driver.update_point_table(updated, timeout=5.0)
    assert unchanged.status is PointTableUpdateStatus.UNCHANGED
    assert unchanged.applied is False
    assert unchanged.active_version == 2

    # 再一次成功变更：代次继续递增，不与 YAML/表 ID 混用。
    updated_again = _table("t3", {"p0": _point("p0", 0), "p1": _point("p1", 2)})
    again = await driver.update_point_table(updated_again, timeout=5.0)
    assert again.status is PointTableUpdateStatus.SUCCESS
    assert again.active_version == 3


async def test_st_53_validation_failure_reports_issue_and_keeps_mapping() -> None:
    base = _table("t1", {"p0": _point("p0", 0)})
    driver = _driver(base)
    driver._session = _OkSession()
    original_points = dict(driver._points)

    bad = _table("t-bad", {"p0": _point("p0", 0), "p_bad": _point("p_bad", 65536)})
    result = await driver.update_point_table(bad, timeout=5.0)

    assert result.status is PointTableUpdateStatus.VALIDATION_FAILED
    assert result.applied is False
    assert result.active_version == 1
    assert result.resource_state == "READY"
    # 当前合同：编译期校验失败把点位与原因写入 message；
    # point_errors 在该路径为空（PointTableIssue(phase='validation') 未被
    # Modbus 驱动使用）——差距已在 S3 报告登记为移交项，测试不虚造逐点结构。
    assert "p_bad" in result.message
    assert result.point_errors == ()
    # 映射未切换：原表对象、原点位完全保留。
    assert driver._point_table is base
    assert driver._points == original_points


async def test_st_26_not_ready_when_session_absent() -> None:
    base = _table("t1", {"p0": _point("p0", 0)})
    driver = _driver(base)  # 不注入 session：驱动未连接
    new_table = _table("t2", {"p1": _point("p1", 9)})

    result = await driver.update_point_table(new_table, timeout=5.0)
    assert result.status is PointTableUpdateStatus.NOT_READY
    assert result.applied is False
    assert result.active_version == 1
    assert result.resource_state == "FAULTED"
    assert driver._point_table is base


async def test_st_26_timeout_does_not_switch_mapping() -> None:
    base = _table("t1", {"p0": _point("p0", 0)})
    driver = _driver(base)
    driver._session = _BlockingSession()
    new_table = _table("t2", {"p1": _point("p1", 5)})

    result = await driver.update_point_table(new_table, timeout=0.05)
    assert result.status is PointTableUpdateStatus.TIMEOUT
    assert result.applied is False
    assert result.active_version == 1
    assert driver._point_table is base
    assert set(driver._points) == {"p0"}


async def test_st_26_partial_application_lists_unverified_nonreadable_points() -> None:
    base = _table("t1", {"p0": _point("p0", 0)})
    driver = _driver(base)
    driver._session = _OkSession()
    # 新增一个只写线圈：变更成立但无法在线读校验，必须进入 unverified_points；
    # 可读点 p1 正常验证，整笔事务仍可提交（部分应用如实标注，不虚报全验证）。
    new_table = _table(
        "t2",
        {
            "p0": _point("p0", 0),
            "p1": _point("p1", 1),
            "c1": _point("c1", 8, access=PointAccess.WRITE, register_type="coil", data_type="bool"),
        },
    )
    result = await driver.update_point_table(new_table, timeout=5.0)
    assert result.status is PointTableUpdateStatus.SUCCESS
    assert result.applied is True
    assert result.unverified_points == ("c1",)
    assert "c1" not in driver._readable and "c1" in driver._writable


async def test_st_54_verification_failure_never_switches_mapping_or_plans() -> None:
    base = _table("t1", {"p0": _point("p0", 0), "p1": _point("p1", 1)})
    driver = _driver(base)
    ok = _OkSession()
    driver._session = ok
    # 先建立受影响/不受影响两类读取计划缓存。
    await driver.read_many(["p0"])
    await driver.read_many(["p1"])
    cached_p0 = driver._read_plan_cache[("p0",)]
    cached_p1 = driver._read_plan_cache[("p1",)]
    original_table = driver._point_table

    # 验证期对端故障：p1 改地址后的在线读确认失败。
    driver._session = _FailingSession(OSError("wire refused"))
    bad_update = _table("t2", {"p0": _point("p0", 0), "p1": _point("p1", 7)})
    result = await driver.update_point_table(bad_update, timeout=5.0)

    assert result.status is PointTableUpdateStatus.VERIFICATION_FAILED
    assert result.applied is False
    assert result.active_version == 1
    assert all(issue.phase == "verification" for issue in result.point_errors)
    assert {issue.point_id for issue in result.point_errors} == {"p1"}
    # 提交前故障：映射、表对象与既有计划全部保留，无半应用状态。
    assert driver._point_table is original_table
    assert driver._points["p1"].address == 1
    assert driver._read_plan_cache[("p0",)] is cached_p0
    assert driver._read_plan_cache[("p1",)] is cached_p1

    # 故障恢复后同一更新可正常提交并差异化淘汰受影响计划。
    driver._session = ok
    recovered = await driver.update_point_table(bad_update, timeout=5.0)
    assert recovered.status is PointTableUpdateStatus.SUCCESS
    assert recovered.active_version == 2
    assert ("p1",) not in driver._read_plan_cache
    assert driver._read_plan_cache[("p0",)] is cached_p0


async def test_st_54_invalid_timeout_arguments_rejected_before_any_io() -> None:
    driver = _driver(_table("t1", {"p0": _point("p0", 0)}))
    new_table = _table("t2", {"p1": _point("p1", 1)})
    with pytest.raises(ValueError, match="timeout"):
        await driver.update_point_table(new_table, timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        await driver.update_point_table(new_table, timeout=True)  # type: ignore[arg-type]
    assert driver._active_version == 1
