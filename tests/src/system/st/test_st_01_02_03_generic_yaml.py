"""Core v1_31 ST-01/02/03：通用 YAML 原语与结构化配置差异（设计 §14.3）。

直接覆盖 ``core.infrastructure.config.generic_yaml`` 的安全解析、原子
保存往返和确定性差异比较，不经过任何服务装配。层级 marker 固定为
``system``；场景 marker 按 ST 表逐项施加（ST-02 为 safety/invalid_input）。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from core.infrastructure.config import (
    MISSING,
    ConfigErrorCode,
    ConfigOperationError,
    compare_config,
    parse_yaml,
    read_yaml,
    save_yaml,
)

pytestmark = pytest.mark.system


# ---------------------------------------------------------------------------
# ST-01：任意路径 YAML 保存/读取往返等价，无默认目录依赖
# ---------------------------------------------------------------------------


def test_st01_save_and_read_round_trip_preserves_content(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "document.yaml"
    target.parent.mkdir()  # save_yaml 只做原子替换，不隐式创建父目录
    payload = {
        "schema_version": 1,
        "devices": {"TZ01": {"endpoint": {"host": "10.0.0.1", "port": 1502}}},
        "tags": ["a", "b", "中文"],
        "ratio": 0.5,
    }

    save_yaml(target, payload)
    assert target.is_file()
    assert read_yaml(target) == payload
    assert parse_yaml(target.read_text(encoding="utf-8")) == payload


def test_st01_round_trip_does_not_mutate_input(tmp_path: Path) -> None:
    payload = {"outer": {"inner": [1, 2, {"k": "v"}]}}
    snapshot = copy.deepcopy(payload)

    save_yaml(tmp_path / "doc.yaml", payload)

    assert payload == snapshot


def test_st01_read_missing_file_is_classified_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigOperationError) as exc_info:
        read_yaml(tmp_path / "absent.yaml")
    assert exc_info.value.code is ConfigErrorCode.NOT_FOUND
    assert exc_info.value.path == tmp_path / "absent.yaml"


# ---------------------------------------------------------------------------
# ST-02：重复键、错误键型、非法值有可定位的解析报错，不产生错误目标
# ---------------------------------------------------------------------------


@pytest.mark.safety
@pytest.mark.invalid_input
def test_st02_duplicate_mapping_key_rejected() -> None:
    with pytest.raises(ConfigOperationError) as exc_info:
        parse_yaml("key: 1\nkey: 2\n")
    assert exc_info.value.code is ConfigErrorCode.PARSE_ERROR


@pytest.mark.safety
@pytest.mark.invalid_input
@pytest.mark.parametrize(
    "document",
    [
        "- just",
        "- a\n- list\n",
        "scalar-value\n",
        "123\n",
    ],
)
def test_st02_non_mapping_root_rejected(document: str) -> None:
    with pytest.raises(ConfigOperationError) as exc_info:
        parse_yaml(document)
    assert exc_info.value.code is ConfigErrorCode.PARSE_ERROR


@pytest.mark.safety
@pytest.mark.invalid_input
def test_st02_non_string_key_rejected() -> None:
    with pytest.raises(ConfigOperationError):
        parse_yaml("1: value\n")


@pytest.mark.safety
@pytest.mark.invalid_input
def test_st02_non_finite_float_rejected() -> None:
    with pytest.raises(ConfigOperationError) as exc_info:
        parse_yaml("value: .nan\n")
    assert exc_info.value.code is ConfigErrorCode.PARSE_ERROR


@pytest.mark.safety
@pytest.mark.invalid_input
def test_st02_cyclic_structure_rejected_on_save(tmp_path: Path) -> None:
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    with pytest.raises(ConfigOperationError) as exc_info:
        save_yaml(tmp_path / "cyclic.yaml", cyclic)
    assert exc_info.value.code is ConfigErrorCode.INVALID_INPUT
    assert not (tmp_path / "cyclic.yaml").exists()


@pytest.mark.safety
@pytest.mark.invalid_input
def test_st02_empty_document_rejected() -> None:
    with pytest.raises(ConfigOperationError):
        parse_yaml("")


# ---------------------------------------------------------------------------
# ST-03：嵌套结构差异正确且不修改输入
# ---------------------------------------------------------------------------


def test_st03_diff_classifies_added_removed_and_changed_paths() -> None:
    old = {
        "keep": 1,
        "removed": {"nested": 2},
        "changed_scalar": 1,
        "items": [0, 1, 2],
    }
    new = {
        "keep": 1,
        "added": ["x"],
        "changed_scalar": 2,
        "items": [0, 9, 2, 3],
    }

    diff = compare_config(old, new)

    assert diff.has_changes
    index = {(entry.path, entry.kind): entry for entry in diff.entries}
    added = index[(("added",), "added")]
    assert added.old_value is MISSING and added.new_value == ["x"]
    # 整键缺失时差异停在缺失键本身，不向下递归。
    removed = index[(("removed",), "removed")]
    assert removed.old_value == {"nested": 2} and removed.new_value is MISSING
    changed = index[(("changed_scalar",), "changed")]
    assert (changed.old_value, changed.new_value) == (1, 2)
    list_changed = index[(("items", 1), "changed")]
    assert (list_changed.old_value, list_changed.new_value) == (1, 9)
    list_added = index[(("items", 3), "added")]
    assert list_added.new_value == 3


def test_st03_diff_is_deterministic_across_key_order() -> None:
    old = {"b": 1, "a": {"z": 1, "y": 2}}
    new = {"a": {"y": 2, "z": 9}, "b": 1, "c": True}

    first = compare_config(old, new)
    second = compare_config(old, new)

    assert first == second
    assert [entry.path for entry in first.entries] == sorted(
        (entry.path for entry in first.entries), key=lambda part: tuple(map(str, part))
    )


def test_st03_diff_distinguishes_value_types_and_equal_documents() -> None:
    diff = compare_config({"v": 1}, {"v": True})
    assert len(diff.entries) == 1 and diff.entries[0].kind == "changed"

    assert not compare_config({"a": [1, 2]}, {"a": [1, 2]}).has_changes


def test_st03_diff_never_mutates_inputs() -> None:
    old = {"devices": {"TZ01": {"port": 1}}}
    new = {"devices": {"TZ01": {"port": 2}, "TZ02": {"port": 3}}}
    old_snapshot = copy.deepcopy(old)
    new_snapshot = copy.deepcopy(new)

    compare_config(old, new)

    assert old == old_snapshot
    assert new == new_snapshot
