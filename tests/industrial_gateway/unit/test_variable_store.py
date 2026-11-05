"""VariableStore v1 单元契约（迁移自旧 PointStore 业务场景）。

v1 语义变化（设计 v1_9 重构，非缺陷）：
- 变量以全局 variable_id 寻址，不再有 (device, point) 双键与角色；
- 批次不是事务：逐点独立成败，先成功者保留（旧 PointStore 批次原子性场景按新语义重写）；
- 无冷启动镜像/反馈失效概念：未初始化即 value=None/version=0；
- 配置热重载仅保留类型兼容的既有值。
"""

from __future__ import annotations

import math

import pytest

from industrial_gateway.application.variable_store import (
    VariableStore,
    validate_value,
)

pytestmark = pytest.mark.unit


async def test_uninitialized_variable_reads_none_at_version_zero() -> None:
    store = VariableStore({"p1": "float32"})
    snapshot = store.read("p1")
    assert snapshot.value is None
    assert snapshot.version == 0
    assert snapshot.updated_at is None
    with pytest.raises(KeyError):
        store.read("missing")


async def test_each_update_increments_version_including_same_value() -> None:
    store = VariableStore({"p1": "uint16"})
    first = await store.update("p1", 7)
    second = await store.update("p1", 7)
    assert (first.version, second.version) == (1, 2)
    assert store.read("p1").value == 7


async def test_unknown_variable_update_raises_without_side_effects() -> None:
    store = VariableStore({"p1": "uint16"})
    with pytest.raises(KeyError):
        await store.update("missing", 1)


@pytest.mark.parametrize(
    ("data_type", "good", "bad", "label"),
    [
        ("bool", True, 1, "bool"),
        ("int8", -128, -129, "range"),
        ("uint8", 255, 256, "range"),
        ("int16", -(1 << 15), -(1 << 15) - 1, "range"),
        ("uint16", (1 << 16) - 1, 1 << 16, "range"),
        ("int32", -(1 << 31), -(1 << 31) - 1, "range"),
        ("uint32", (1 << 32) - 1, 1 << 32, "range"),
        ("int64", -(1 << 63), -(1 << 63) - 1, "range"),
        ("uint64", (1 << 64) - 1, 1 << 64, "range"),
        ("float32", 1.5, float("nan"), "finite"),
        ("float64", 1.5, float("inf"), "finite"),
        ("str", "running", 1, "str"),
        ("bytes", b"\x01", "x", "bytes"),
    ],
)
def test_value_validation_boundaries(data_type: str, good: object, bad: object, label: str) -> None:
    validate_value(data_type, good)
    with pytest.raises((TypeError, ValueError), match=label):
        validate_value(data_type, bad)


def test_float32_rejects_out_of_range_and_non_finite() -> None:
    validate_value("float32", 3.4e38)
    with pytest.raises(ValueError, match="float32"):
        validate_value("float32", 3.5e38)
    with pytest.raises(ValueError, match="finite"):
        validate_value("float32", math.inf)


def test_unknown_data_type_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_value("float16", 1.0)


async def test_update_many_reports_per_item_results_and_keeps_successes() -> None:
    store = VariableStore({"p1": "int32", "p2": "float32", "p3": "uint16"})
    results = await store.update_many(
        [
            ("p1", 5),
            ("p2", 9.0),  # float into float32: OK
            ("p1", 2.0),  # type mismatch: INVALID_VALUE
            ("missing", 1),  # NOT_FOUND
            ("p3", 70000),  # range: INVALID_VALUE
            ("p3", 42),
        ]
    )
    assert [(r.variable_id, r.success, r.error_code) for r in results] == [
        ("p1", True, ""),
        ("p2", True, ""),
        ("p1", False, "INVALID_VALUE"),
        ("missing", False, "NOT_FOUND"),
        ("p3", False, "INVALID_VALUE"),
        ("p3", True, ""),
    ]
    # 批次非事务：先成的 p1=5 与最后的 p3=42 必须保留，坏项不回滚好项。
    assert store.read("p1").value == 5
    assert store.read("p2").value == 9.0
    assert store.read("p3").value == 42


async def test_per_variable_locks_serialize_concurrent_updates() -> None:
    store = VariableStore({"p1": "uint32"})
    import asyncio

    await asyncio.gather(*(store.update("p1", i) for i in range(50)))
    assert store.read("p1").version == 50


def test_seed_from_keeps_only_type_compatible_values() -> None:
    old = VariableStore({"keep": "uint16", "typed": "float32"})
    # 直接植入快照模拟重载前状态。
    import asyncio

    async def prepare() -> None:
        await old.update("keep", 100)
        await old.update("typed", 3.0)

    asyncio.run(prepare())
    new = VariableStore({"keep": "uint16", "typed": "uint16", "added": "uint16"})
    new.seed_from(old)
    assert new.read("keep").value == 100  # 同类型保留
    assert new.read("typed").value is None  # float 不可作 uint16，丢弃
    assert new.read("added").value is None  # 新增点未初始化
    assert new.read("keep").version == 1
    # 旧存储中存在、新配置删除的点不会迁移（新类型表中无此键）。
    removed = VariableStore({"keep": "uint16", "dropped": "uint16"})
    removed.seed_from(old)
    with pytest.raises(KeyError):
        removed.read("typed")
