"""S2-6/7/8/10：source_manifest 确定性、引用闭包原始字节复验、越界拒绝与
显式子集加载。

全部使用真实文件系统与真实 YAML 资产，不 mock 文件身份或锁原语。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from configuration.document_version import (
    read_stable_document,
    verify_shared_source_closure,
)
from configuration.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
)
from core.application.errors import ConfigError

pytestmark = [pytest.mark.component, pytest.mark.contract]

_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def _repo_paths(*, load_site: bool = True) -> SharedConfigPaths:
    return SharedConfigPaths(
        instances=_REPO_CONFIGS / "instances",
        templates=_REPO_CONFIGS / "templates",
        site_file=_REPO_CONFIGS / "instances" / "site.yaml",
        devices_file=_REPO_CONFIGS / "instances" / "devices.yaml",
        load_site=load_site,
    )


# ---------------------------------------------------------------------------
# S2-8：source_manifest 完整、排序确定、版本与原始字节 SHA-256 正确
# ---------------------------------------------------------------------------


def test_source_manifest_is_complete_sorted_and_byte_accurate() -> None:
    loader = SharedDocumentLoader(_repo_paths())
    documents = loader.load()

    manifest = documents.source_manifest
    assert manifest  # 闭包非空：site/devices/units/business_points + 全部引用模板
    logical_keys = [identity.logical_key for identity in manifest]
    assert logical_keys == sorted(logical_keys)
    assert len(set(logical_keys)) == len(logical_keys)

    expected_sources = (
        "instances/site.yaml",
        "instances/devices.yaml",
        "templates/units.yaml",
        "templates/business_points.yaml",
    )
    for relative in expected_sources:
        assert relative in logical_keys, relative

    for identity in manifest:
        physical = _REPO_CONFIGS / identity.logical_key
        assert identity.path == physical.absolute()
        assert identity.schema_version == 1
        document = yaml.safe_load(physical.read_text(encoding="utf-8"))
        assert identity.file_version == document["file_version"]
        assert identity.sha256 == hashlib.sha256(physical.read_bytes()).hexdigest()
        assert not identity.logical_key.startswith("/")
        assert ".." not in identity.logical_key.split("/")

    # 确定性：重复加载给出逐元素相同的 manifest。
    again = SharedDocumentLoader(_repo_paths()).load()
    assert again.source_manifest == manifest
    assert again.revision == documents.revision


# ---------------------------------------------------------------------------
# S2-6：整份引用闭包再次校验原始 SHA-256；仅改空白、YAML 语义不变也必须拒绝
# ---------------------------------------------------------------------------


def test_closure_rejects_whitespace_only_change_with_equal_yaml_semantics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "devices.yaml"
    original = (_REPO_CONFIGS / "instances" / "devices.yaml").read_bytes()
    source.write_bytes(original)

    with pytest.raises(ConfigError, match="SOURCE_CHANGED"), verify_shared_source_closure():
        before = read_stable_document(source)
        semantic_before = yaml.safe_load(source.read_text(encoding="utf-8"))
        # 仅追加空行与注释：YAML 语义完全相同，原始字节身份必须仍然变化。
        source.write_bytes(original + b"\n\n# whitespace-only change\n")
        semantic_after = yaml.safe_load(source.read_text(encoding="utf-8"))
        assert semantic_after == semantic_before
        assert before["file_version"]

    # 稳定读取本身在文件不变时通过并登记身份。
    with verify_shared_source_closure() as identities:
        read_stable_document(source)
    assert source.absolute() in identities


def test_read_stable_document_rejects_replacement_inside_single_read_window(
    tmp_path: Path,
) -> None:
    source = tmp_path / "devices.yaml"
    original = (_REPO_CONFIGS / "instances" / "devices.yaml").read_bytes()
    source.write_bytes(original)
    # 同语义、不同字节的重写必须被前后两次 read_bytes 对比捕获。
    with pytest.MonkeyPatch.context() as monkeypatch:
        calls = {"n": 0}
        original_read_bytes = Path.read_bytes

        def flapping_read_bytes(self: Path) -> bytes:
            if self == source:
                calls["n"] += 1
                if calls["n"] == 2:
                    return original + b"# replaced between reads\n"
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", flapping_read_bytes)
        with pytest.raises(ConfigError, match="SOURCE_CHANGED"):
            read_stable_document(source)


# ---------------------------------------------------------------------------
# S2-7：读取过程中被删除/移出协调根/位于协调根之外，明确拒绝
# ---------------------------------------------------------------------------


def test_closure_rejects_deleted_source(tmp_path: Path) -> None:
    source = tmp_path / "devices.yaml"
    source.write_bytes((_REPO_CONFIGS / "instances" / "devices.yaml").read_bytes())
    with pytest.raises(ConfigError, match="SOURCE_CHANGED"), verify_shared_source_closure():
        read_stable_document(source)
        source.unlink()


def test_closure_rejects_source_moved_out_of_root(tmp_path: Path) -> None:
    source = tmp_path / "devices.yaml"
    source.write_bytes((_REPO_CONFIGS / "instances" / "devices.yaml").read_bytes())
    moved_to = tmp_path / "moved-devices.yaml"
    with pytest.raises(ConfigError, match="SOURCE_CHANGED"), verify_shared_source_closure():
        read_stable_document(source)
        source.rename(moved_to)
    moved_to.unlink()


def test_loader_rejects_sources_outside_coordination_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    shutil.copytree(_REPO_CONFIGS, external)
    coordination_root = tmp_path / "coord"
    coordination_root.mkdir()
    paths = SharedConfigPaths(
        instances=external / "instances",
        templates=external / "templates",
        site_file=external / "instances" / "site.yaml",
        devices_file=external / "instances" / "devices.yaml",
        coordination_root=coordination_root,
    )

    with pytest.raises(ConfigError, match="INVALID_PATH"):
        SharedDocumentLoader(paths).load()


# ---------------------------------------------------------------------------
# S2-10：load() 缺显式文件拒绝；load_devices() 显式子集加载语义
# ---------------------------------------------------------------------------


def test_load_devices_resolves_only_selected_subset_without_site() -> None:
    paths = _repo_paths(load_site=False)
    devices_file = _REPO_CONFIGS / "instances" / "devices.yaml"
    documents = SharedDocumentLoader(paths).load_devices(
        devices_file,
        selected_device_ids={"TZ01"},
    )

    assert set(documents.devices) == {"TZ01"}
    # load_devices 为无 site 子集路径：site 显式为空映射，不回退任何默认文件。
    assert dict(documents.site) == {}
    assert documents.devices_file_version
    # 子集路径同样产出完整闭包 manifest。
    assert documents.source_manifest
    assert [i.logical_key for i in documents.source_manifest] == sorted(
        i.logical_key for i in documents.source_manifest
    )


def test_load_devices_without_selection_loads_full_catalog() -> None:
    paths = _repo_paths(load_site=False)
    devices_file = _REPO_CONFIGS / "instances" / "devices.yaml"
    documents = SharedDocumentLoader(paths).load_devices(devices_file)
    assert "TZ01" in documents.devices and "GW01" in documents.devices


def test_load_devices_rejects_unknown_subset_id() -> None:
    paths = _repo_paths(load_site=False)
    devices_file = _REPO_CONFIGS / "instances" / "devices.yaml"
    with pytest.raises(ConfigError, match="configured device IDs not found"):
        SharedDocumentLoader(paths).load_devices(
            devices_file,
            selected_device_ids={"NO-SUCH-DEVICE"},
        )
