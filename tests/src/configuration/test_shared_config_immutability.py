"""Configuration v1_9 深层不可变契约专项（任务书 S2 第 1-10 项）。

双路径覆盖：
- 直接构造：freeze_document/plain_document、SharedDocuments、
  SharedDeviceDefinition、ResolvedTemplate 的输入脱离与逐层冻结；
- 真实共享加载：SharedDocumentLoader 读取仓库 configs 真实 YAML 资产，
  build_shared_snapshot 构建类型化 SharedDomainSnapshot 后五个公开索引
  仍为只读映射，revision/哈希在冻结前后保持稳定。

全部断言针对真实类型行为（MappingProxyType/tuple），不使用 mock，
不依赖网络与外部服务。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

from configuration.immutable import freeze_document, plain_document
from configuration.shared_documents import (
    SharedConfigPaths,
    SharedDeviceDefinition,
    SharedDocumentLoader,
    SharedDocuments,
)
from configuration.shared_snapshot import SharedDomainSnapshot, build_shared_snapshot
from configuration.template_graph import DeviceTemplates
from configuration.versioned_templates import (
    ResolvedTemplate,
    TemplateRef,
    config_hash,
)

pytestmark = [pytest.mark.component, pytest.mark.contract]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_CONFIGS = _REPO_ROOT / "configs"


def _templates(document: dict[str, Any]) -> DeviceTemplates:
    ref = TemplateRef("device_models", "model-under-test", "1.0.0")
    template = ResolvedTemplate(
        reference=ref,
        path=Path("/templates/device_models/model-under-test_1.0.0.yaml"),
        content_hash="hash-1",
        document=document,
    )
    return DeviceTemplates(
        model=template,
        protocol=template,
        point_table=template,
        inherited_tables=(),
    )


def test_freeze_document_wraps_every_nested_layer() -> None:
    """S2-1/2：dict→MappingProxy、list→tuple，嵌套层逐层冻结。"""
    frozen = freeze_document({"a": {"b": [1, {"c": 2}]}, "d": [3, 4]})

    assert isinstance(frozen, MappingProxyType)
    with pytest.raises(TypeError):
        frozen["a"] = {}  # type: ignore[index]
    assert isinstance(frozen["a"], MappingProxyType)
    with pytest.raises(TypeError):
        frozen["a"]["b"] = ()  # type: ignore[index]
    nested_list = frozen["a"]["b"]
    assert isinstance(nested_list, tuple) and nested_list == (1, {"c": 2})
    assert isinstance(nested_list[1], MappingProxyType)
    with pytest.raises((TypeError, AttributeError)):
        nested_list[1]["c"] = 9
    assert frozen["d"] == (3, 4) and isinstance(frozen["d"], tuple)


def test_freeze_detaches_from_caller_owned_mutable_input() -> None:
    """S2-6：冻结后修改原始输入 dict/list，不改变已构建快照。"""
    raw: dict[str, Any] = {"a": [1, 2], "b": {"c": 3}}
    frozen = freeze_document(raw)

    raw["a"].append(99)
    raw["b"]["c"] = 100
    raw["new"] = "leak"

    assert tuple(frozen["a"]) == (1, 2)
    assert frozen["b"]["c"] == 3
    assert "new" not in frozen


def test_plain_document_returns_independent_mutable_copy() -> None:
    """S2-7：plain_document 产出独立普通 dict/list，改其结果不影响原快照。"""
    frozen = freeze_document({"a": [{"b": 1}], "c": {"d": [2]}})
    plain = plain_document(frozen)

    assert type(plain) is dict
    assert type(plain["a"]) is list
    assert type(plain["a"][0]) is dict
    assert type(plain["c"]["d"]) is list

    plain["a"].append({"b": 2})
    plain["c"]["d"].append(9)
    assert frozen["a"] == ({"b": 1},)
    assert frozen["c"]["d"] == (2,)

    # 普通容器可直接被 JSON/YAML 序列化；MappingProxy 不允许直传 JSON。
    json.dumps(plain, allow_nan=False)
    yaml.safe_dump(plain, allow_unicode=True)
    with pytest.raises(TypeError):
        json.dumps(frozen)


def test_freeze_is_idempotent_and_hash_stable() -> None:
    """S2-8：冻结/解冻往返内容一致，config_hash 与键顺序无关且对冻结稳定。"""
    source = {"b": 2, "a": [1, 2, 3], "nested": {"z": 1, "y": 2}}
    frozen = freeze_document(source)
    assert freeze_document(frozen) == frozen
    assert plain_document(frozen) == source

    assert config_hash(source) == config_hash(frozen)
    assert config_hash(source) == config_hash(plain_document(frozen))
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
    # 内容变化必须体现在哈希上。
    assert config_hash(source) != config_hash({**source, "b": 3})


def test_shared_device_definition_endpoint_and_options_are_frozen() -> None:
    """S2-3：SharedDeviceDefinition 的 endpoint/protocol_options 逐层不可变。"""
    endpoint: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 1502,
        "aliases": ["a", "b"],
    }
    options: dict[str, Any] = {"unit_id": 1, "nested": {"timeout_ms": 500}}
    definition = SharedDeviceDefinition(
        device_id="WT01",
        name="turbine 1",
        templates=_templates({"template_id": "model-under-test"}),
        endpoint=endpoint,
        protocol_options=options,
        enabled=True,
    )

    assert isinstance(definition.endpoint, MappingProxyType)
    assert isinstance(definition.endpoint["aliases"], tuple)
    assert isinstance(definition.protocol_options["nested"], MappingProxyType)
    with pytest.raises(TypeError):
        definition.endpoint["port"] = 9  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        definition.protocol_options["nested"]["timeout_ms"] = 1  # type: ignore[index]

    # 脱离输入：调用方继续持有可变 dict，其修改不进入定义对象。
    endpoint["port"] = 9999
    options["nested"]["timeout_ms"] = 1
    assert definition.endpoint["port"] == 1502
    assert definition.protocol_options["nested"]["timeout_ms"] == 500


def test_shared_documents_direct_construction_freezes_all_indexes() -> None:
    """S2-1/2/6：SharedDocuments 顶层与嵌套结构冻结，且脱离加载方输入。"""
    site: dict[str, Any] = {"site_id": "site-1", "nested": {"x": [1]}}
    units: dict[str, Any] = {"units": {"u": {"symbol": "m"}}}
    business_points: dict[str, Any] = {"points": {"bp": {"unit": "m"}}}
    definition = SharedDeviceDefinition(
        device_id="WT01",
        name=None,
        templates=_templates({"template_id": "model-under-test"}),
        endpoint={"host": "127.0.0.1"},
        protocol_options={},
        enabled=True,
    )
    documents = SharedDocuments(
        site=site,
        devices={"WT01": definition},
        units=units,
        business_points=business_points,
        revision="rev-1",
        devices_file_version="1.0.0",
    )

    for name in ("site", "units", "business_points", "devices"):
        assert isinstance(getattr(documents, name), MappingProxyType), name
    assert isinstance(documents.site["nested"]["x"], tuple)
    with pytest.raises(TypeError):
        documents.site["site_id"] = "other"  # type: ignore[index]
    with pytest.raises(TypeError):
        documents.devices["WT02"] = definition  # type: ignore[index]

    site["nested"]["x"].append(2)
    units["units"]["u"]["symbol"] = "changed"
    assert documents.site["nested"]["x"] == (1,)
    assert documents.units["units"]["u"]["symbol"] == "m"


def test_resolved_template_freezes_document_on_direct_construction() -> None:
    """S2-4（直接构造路径）：ResolvedTemplate 构建即冻结并脱离源映射。"""
    raw_document: dict[str, Any] = {
        "template_id": "pt-1",
        "file_version": "1.0.0",
        "points": {"p1": {"address": [0, 1]}},
    }
    template = ResolvedTemplate(
        reference=TemplateRef("point_tables", "pt-1", "1.0.0"),
        path=Path("/templates/point_tables/pt-1_1.0.0.yaml"),
        content_hash=config_hash(raw_document),
        document=raw_document,
    )

    assert isinstance(template.document, MappingProxyType)
    assert isinstance(template.document["points"]["p1"]["address"], tuple)
    raw_document["points"]["p1"]["address"].append(2)
    raw_document["injected"] = True
    assert template.document["points"]["p1"]["address"] == (0, 1)
    assert "injected" not in template.document
    with pytest.raises(TypeError):
        template.document["template_id"] = "pt-2"  # type: ignore[index]


def test_shared_template_consumers_cannot_affect_each_other() -> None:
    """S2-9：多设备共用同一模板，任一方拿到的 plain 副本互不影响且源不可变。"""
    template = ResolvedTemplate(
        reference=TemplateRef("device_models", "shared-model", "1.0.0"),
        path=Path("/templates/device_models/shared-model_1.0.0.yaml"),
        content_hash="hash-shared",
        document={"protocol_options": {"timeout_ms": 500}, "tags": ["a"]},
    )
    first = SharedDeviceDefinition(
        device_id="WT01",
        name=None,
        templates=DeviceTemplates(template, template, template, ()),
        endpoint={"host": "10.0.0.1"},
        protocol_options={"unit_id": 1},
        enabled=True,
    )
    second = SharedDeviceDefinition(
        device_id="WT02",
        name=None,
        templates=DeviceTemplates(template, template, template, ()),
        endpoint={"host": "10.0.0.2"},
        protocol_options={"unit_id": 2},
        enabled=True,
    )

    plain_one = plain_document(first.templates.model.document)
    plain_two = plain_document(second.templates.model.document)
    plain_one["tags"].append("b")
    plain_one["protocol_options"]["timeout_ms"] = 1
    assert plain_two["tags"] == ["a"]
    assert plain_two["protocol_options"]["timeout_ms"] == 500
    # 共享模板本体始终不可变。
    assert first.templates.model.document["tags"] == ("a",)
    assert second.templates.model is first.templates.model


def test_real_loader_builds_immutable_documents_and_domain_snapshot() -> None:
    """S2-4/5/10（仓库加载路径）：真实 YAML 资产加载后全链路只读且可重复。"""
    paths = SharedConfigPaths(
        instances=_REPO_CONFIGS / "instances",
        templates=_REPO_CONFIGS / "templates",
        load_site=False,
        devices_file=_REPO_CONFIGS / "instances" / "devices.yaml",
    )
    loader = SharedDocumentLoader(paths)
    documents = loader.load()

    assert isinstance(documents.site, MappingProxyType)
    assert isinstance(documents.devices, MappingProxyType)
    assert isinstance(documents.units, MappingProxyType)
    assert isinstance(documents.business_points, MappingProxyType)
    first_device = next(iter(documents.devices.values()))
    assert isinstance(first_device.endpoint, MappingProxyType)
    assert isinstance(first_device.protocol_options, MappingProxyType)
    assert isinstance(first_device.templates.model.document, MappingProxyType)
    with pytest.raises(TypeError):
        documents.business_points["injected"] = {}  # type: ignore[index]

    # 重复加载给出相同 revision（哈希确定性），且两份文档互不共享可变容器。
    again = loader.load()
    assert again.revision == documents.revision
    assert again.devices_file_version == documents.devices_file_version

    snapshot = build_shared_snapshot(documents)
    assert isinstance(snapshot, SharedDomainSnapshot)
    for name in (
        "device_types",
        "device_models",
        "device_groups",
        "point_tables",
        "business_points",
    ):
        assert isinstance(getattr(snapshot, name), MappingProxyType), name
    with pytest.raises(TypeError):
        snapshot.device_models["injected"] = object()  # type: ignore[index]
    assert snapshot.revision == documents.revision

    # 只读快照必须能经 plain_document 安全进入 JSON/YAML 序列化链路。
    json.dumps(plain_document(documents.units), allow_nan=False)
    yaml.safe_dump(plain_document(documents.business_points), allow_unicode=True)
