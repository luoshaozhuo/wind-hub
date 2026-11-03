"""VersionedTemplateRepository 路径安全与精确版本拒绝矩阵。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.template_graph import validate_point_table_chain
from core.infrastructure.config.versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
)

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def _write_flat(root: Path, category: str, name: str, body: str = "") -> Path:
    directory = root / category
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}_1.0.0.yaml"
    path.write_text(
        f"schema_version: 1\ntemplate_id: {name}\nfile_version: 1.0.0\n{body}",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", "1abc", "", "x/y.yaml"])
def test_template_ref_rejects_illegal_id(bad_id: str) -> None:
    with pytest.raises(ConfigError, match="invalid template ID"):
        TemplateRef("protocols", bad_id, "1.0.0")


@pytest.mark.parametrize("bad_version", ["1.0", "latest", "1", "v1.0.0", "1.0.0-rc1"])
def test_template_ref_rejects_illegal_version(bad_version: str) -> None:
    with pytest.raises(ConfigError, match="invalid template version"):
        TemplateRef("protocols", "modbus_tcp", bad_version)


def test_template_ref_rejects_unknown_category() -> None:
    with pytest.raises(ConfigError, match="unsupported template category"):
        TemplateRef("etc", "passwd", "1.0.0")


def test_resolve_missing_flat_file_raises_not_found(tmp_path: Path) -> None:
    repository = VersionedTemplateRepository(tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        repository.resolve(TemplateRef("protocols", "missing", "1.0.0"))


def test_resolve_rejects_template_id_mismatch(tmp_path: Path) -> None:
    # 文件名按请求引用存在，但正文 template_id 不同：必须拒绝。
    path = _write_flat(tmp_path, "protocols", "other_renamed")
    path.write_text(path.read_text(encoding="utf-8").replace("other_renamed", "other"), "utf-8")
    repository = VersionedTemplateRepository(tmp_path)
    with pytest.raises(ConfigError, match="template_id mismatch"):
        repository.resolve(TemplateRef("protocols", "other_renamed", "1.0.0"))


def test_resolve_rejects_file_version_mismatch(tmp_path: Path) -> None:
    path = _write_flat(tmp_path, "protocols", "modbus_tcp")
    # 文件名与引用版本一致，但正文首个 1.0.0（file_version）改为 2.0.0。
    path.write_text(path.read_text(encoding="utf-8").replace("1.0.0", "2.0.0", 1), "utf-8")
    repository = VersionedTemplateRepository(tmp_path)
    with pytest.raises(ConfigError, match="file_version mismatch"):
        repository.resolve(TemplateRef("protocols", "modbus_tcp", "1.0.0"))


def test_flat_categories_use_flat_versioned_filenames() -> None:
    templates = _CONFIGS / "templates"
    for category in ("protocols", "point_tables", "device_models", "tasks"):
        base = templates / category
        assert base.is_dir()
        assert not any(path.is_dir() for path in base.iterdir()), f"{category} 仍含嵌套目录"
        assert list(base.glob("*_1.0.0.yaml")), f"{category} 缺少扁平版本文件"
    assert (templates / "tasks" / "turbine_all_1.0.0.yaml").is_file()


def test_point_table_inheritance_cycle_rejected(tmp_path: Path) -> None:
    _write_flat(
        tmp_path,
        "point_tables",
        "child",
        "protocol: modbus\n" "parent:\n  id: parent\n  version: 1.0.0\npoints: {}\n",
    )
    _write_flat(
        tmp_path,
        "point_tables",
        "parent",
        "protocol: modbus\n" "parent:\n  id: child\n  version: 1.0.0\npoints: {}\n",
    )
    repository = VersionedTemplateRepository(tmp_path)
    root = repository.resolve(TemplateRef("point_tables", "child", "1.0.0"))
    with pytest.raises(ConfigError, match="cycle"):
        validate_point_table_chain(repository, root)


def test_point_table_parent_protocol_mismatch_rejected(tmp_path: Path) -> None:
    _write_flat(
        tmp_path,
        "point_tables",
        "child",
        "protocol: modbus\n" "parent:\n  id: parent\n  version: 1.0.0\npoints: {}\n",
    )
    _write_flat(tmp_path, "point_tables", "parent", "protocol: ads\npoints: {}\n")
    repository = VersionedTemplateRepository(tmp_path)
    root = repository.resolve(TemplateRef("point_tables", "child", "1.0.0"))
    with pytest.raises(ConfigError, match="parent protocol mismatch"):
        validate_point_table_chain(repository, root)
