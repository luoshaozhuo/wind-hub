"""业务无关的单文件 YAML 操作及结构化差异。"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeAlias

import yaml


class ConfigErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    PARSE_ERROR = "PARSE_ERROR"
    SERIALIZATION_ERROR = "SERIALIZATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    IO_ERROR = "IO_ERROR"
    REFERENCE_ERROR = "REFERENCE_ERROR"


class ConfigOperationError(Exception):
    """通用配置错误；replaced 为真表示文件替换已发生。"""

    def __init__(
        self,
        operation: str,
        code: ConfigErrorCode,
        *,
        path: Path | None = None,
        replaced: bool = False,
    ) -> None:
        self.operation = operation
        self.code = code
        self.path = path
        self.replaced = replaced
        location = f" path={path}" if path is not None else ""
        super().__init__(f"{operation}: {code.value}{location} replaced={replaced}")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError("non-string mapping key")
        if key in mapping:
            raise ValueError("duplicate mapping key")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _validate(value: Any, active: set[int]) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return
    if type(value) not in (dict, list):
        raise ValueError("unsupported YAML value")
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic configuration")
    active.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("non-string mapping key")
                _validate(item, active)
        else:
            for item in value:
                _validate(item, active)
    finally:
        active.remove(identity)


def _check_root(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("YAML root must be a mapping")
    _validate(value, set())
    return value


def parse_yaml(content: str) -> dict[str, Any]:
    """安全解析 YAML；空文档、重复键、非 JSON 数据均报错。"""
    if not isinstance(content, str):
        raise ConfigOperationError("parse_yaml", ConfigErrorCode.INVALID_INPUT)
    try:
        return _check_root(yaml.load(content, Loader=_UniqueKeyLoader))
    except (yaml.YAMLError, ValueError, RecursionError, OverflowError) as exc:
        raise ConfigOperationError("parse_yaml", ConfigErrorCode.PARSE_ERROR) from exc


def read_yaml(path: Path) -> dict[str, Any]:
    """仅读取指定文件，不隐式解析其他文件或目录。"""
    if not isinstance(path, Path):
        raise ConfigOperationError("read_yaml", ConfigErrorCode.INVALID_INPUT)
    try:
        return parse_yaml(path.read_text(encoding="utf-8"))
    except ConfigOperationError as exc:
        raise ConfigOperationError("read_yaml", exc.code, path=path) from exc
    except FileNotFoundError as exc:
        raise ConfigOperationError("read_yaml", ConfigErrorCode.NOT_FOUND, path=path) from exc
    except PermissionError as exc:
        raise ConfigOperationError("read_yaml", ConfigErrorCode.PERMISSION_DENIED, path=path) from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigOperationError("read_yaml", ConfigErrorCode.IO_ERROR, path=path) from exc


def dump_yaml(data: dict[str, Any]) -> str:
    """先验证整个数据图，再序列化；不进行文件操作。"""
    try:
        _check_root(data)
    except (ValueError, RecursionError) as exc:
        raise ConfigOperationError("dump_yaml", ConfigErrorCode.INVALID_INPUT) from exc
    try:
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise ConfigOperationError("dump_yaml", ConfigErrorCode.SERIALIZATION_ERROR) from exc


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    """原子替换指定文件，并同步父目录。"""
    if not isinstance(path, Path):
        raise ConfigOperationError("save_yaml", ConfigErrorCode.INVALID_INPUT)
    try:
        rendered = dump_yaml(data)
    except ConfigOperationError as exc:
        raise ConfigOperationError("save_yaml", exc.code, path=path) from exc
    temporary: Path | None = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        code = (
            ConfigErrorCode.PERMISSION_DENIED
            if isinstance(exc, PermissionError)
            else ConfigErrorCode.IO_ERROR
        )
        raise ConfigOperationError("save_yaml", code, path=path, replaced=replaced) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


PathPart: TypeAlias = str | int
DiffKind: TypeAlias = Literal["added", "removed", "changed"]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class ConfigDiffEntry:
    path: tuple[PathPart, ...]
    kind: DiffKind
    old_value: Any
    new_value: Any


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    entries: tuple[ConfigDiffEntry, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.entries)


def _same_scalar(old: Any, new: Any) -> bool:
    return type(old) is type(new) and old == new


def _walk_diff(
    old: Any, new: Any, path: tuple[PathPart, ...], output: list[ConfigDiffEntry],
) -> None:
    if type(old) is dict and type(new) is dict:
        for key in sorted(old.keys() | new.keys()):
            next_path = (*path, key)
            if key not in old:
                output.append(ConfigDiffEntry(next_path, "added", MISSING, new[key]))
            elif key not in new:
                output.append(ConfigDiffEntry(next_path, "removed", old[key], MISSING))
            else:
                _walk_diff(old[key], new[key], next_path, output)
    elif type(old) is list and type(new) is list:
        for index in range(max(len(old), len(new))):
            next_path = (*path, index)
            if index >= len(old):
                output.append(ConfigDiffEntry(next_path, "added", MISSING, new[index]))
            elif index >= len(new):
                output.append(ConfigDiffEntry(next_path, "removed", old[index], MISSING))
            else:
                _walk_diff(old[index], new[index], next_path, output)
    elif not _same_scalar(old, new):
        output.append(ConfigDiffEntry(path, "changed", old, new))


def compare_config(old: dict[str, Any], new: dict[str, Any]) -> ConfigDiff:
    """按键名、列表下标确定性递归比较，不修改输入。"""
    try:
        _check_root(old)
        _check_root(new)
    except (ValueError, RecursionError) as exc:
        raise ConfigOperationError("compare_config", ConfigErrorCode.INVALID_INPUT) from exc
    entries: list[ConfigDiffEntry] = []
    _walk_diff(old, new, (), entries)
    return ConfigDiff(tuple(entries))
