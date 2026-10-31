"""三路径配置、模板精确版本、引用图、继承链与严格拒绝行为。"""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.collector_documents import (
    CollectorDocumentLoader,
)
from core.infrastructure.config.versioned_templates import (
    TemplateRef,
    VersionedTemplateRepository,
)
from tests.collector.config_v2.config_tree import ConfigTree  # noqa: E402

pytestmark = pytest.mark.unit


def _loader(tree) -> CollectorDocumentLoader:
    return CollectorDocumentLoader(tree.paths)


def test_shipped_tree_resolves_exact_versions(config_tree: ConfigTree, documents) -> None:
    wt01 = documents.devices["WT01"]
    assert wt01.model.reference == TemplateRef("device_models", "modbus_wtg", "1.0.0")
    assert wt01.templates["protocol"].reference == TemplateRef("protocols", "modbus_tcp", "1.0.0")
    assert wt01.templates["point_table"].reference == TemplateRef(
        "point_tables", "modbus_wtg", "1.0.0"
    )
    assert wt01.templates["task"].reference == TemplateRef("tasks", "turbine_all", "1.0.0")
    assert wt01.desired_state == "STOP"
    # 精确版本：revision 对同内容稳定。
    again = _loader(config_tree).load()
    assert again.revision == documents.revision


def test_service_change_does_not_alter_instance_resolution(config_tree: ConfigTree) -> None:
    service = config_tree.read("services/collector.yaml")
    service["watchdog"]["interval"] = 9.0
    config_tree.write("services/collector.yaml", service)
    changed = _loader(config_tree).load()
    # 实例/模板解析结果不受 service 文件变化影响。
    assert set(changed.devices) == {"WT01"}


def test_missing_template_reference_rejected(config_tree: ConfigTree) -> None:
    devices = config_tree.read("instances/devices.yaml")
    devices["devices"]["WT01"]["model"]["version"] = "9.9.9"
    config_tree.write("instances/devices.yaml", devices)
    with pytest.raises(ConfigError, match="not found"):
        _loader(config_tree).load()


def test_no_implicit_latest_fallback(config_tree: ConfigTree) -> None:
    # 只有 2.0.0 存在时，引用 1.0.0 必须失败，不允许“取最新”。
    repo = VersionedTemplateRepository(config_tree.templates)
    ref = TemplateRef("protocols", "modbus_tcp", "1.0.0")
    doc = repo.resolve(ref).document
    doc = dict(doc)
    doc["template_version"] = "2.0.0"
    config_tree.write("templates/protocols/modbus_tcp/2.0.0.yaml", doc)
    (config_tree.templates / "protocols/modbus_tcp/1.0.0.yaml").unlink()
    with pytest.raises(ConfigError, match="not found"):
        VersionedTemplateRepository(config_tree.templates).resolve(ref)
    assert (
        VersionedTemplateRepository(config_tree.templates)
        .resolve(TemplateRef("protocols", "modbus_tcp", "2.0.0"))
        .reference.template_version
        == "2.0.0"
    )


def test_template_identifier_and_version_format(config_tree: ConfigTree) -> None:
    repo = VersionedTemplateRepository(config_tree.templates)
    for bad_id in ("../x", "a/b", "1abc", "x y"):
        with pytest.raises(ConfigError):
            repo.path_for(TemplateRef("protocols", bad_id, "1.0.0"))  # type: ignore[arg-type]
    for bad_version in ("1.0", "v1.0.0", "1.0.0.0", "latest"):
        with pytest.raises(ConfigError):
            TemplateRef("protocols", "modbus_tcp", bad_version)  # type: ignore[arg-type]


def test_template_path_cannot_escape_root(config_tree: ConfigTree) -> None:
    repo = VersionedTemplateRepository(config_tree.templates)
    resolved = repo.path_for(TemplateRef("protocols", "modbus_tcp", "1.0.0"))
    assert resolved.is_relative_to(config_tree.templates.resolve())


def test_template_id_version_mismatch_with_file_content(config_tree: ConfigTree) -> None:
    doc = config_tree.read("templates/protocols/modbus_tcp/1.0.0.yaml")
    doc["template_id"] = "different_id"
    config_tree.write("templates/protocols/modbus_tcp/1.0.0.yaml", doc)
    with pytest.raises(ConfigError, match="template_id mismatch"):
        VersionedTemplateRepository(config_tree.templates).resolve(
            TemplateRef("protocols", "modbus_tcp", "1.0.0")
        )


@pytest.mark.parametrize(
    "relative,mutator,match",
    [
        ("instances/devices.yaml", lambda d: d["devices"]["WT01"].update(unknown=1), "unknown"),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["endpoint"].update(x=1),
            "unknown",
        ),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["overrides"].update(x=1),
            "unknown",
        ),
        ("instances/devices.yaml", lambda d: d.update(extra=1), "unknown"),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"].__setitem__("enabled", "yes"),
            "enabled",
        ),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["endpoint"].__setitem__("port", "x"),
            "port",
        ),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["endpoint"].__setitem__("port", 70000),
            "port",
        ),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["model"].__setitem__("id", None),
            "model.id",
        ),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["model"].__setitem__("version", None),
            "version",
        ),
        (
            "instances/devices.yaml",
            lambda d: d["devices"]["WT01"]["model"].update(extra=1),
            "model reference",
        ),
        ("instances/site.yaml", lambda d: d.__setitem__("site_id", "  "), "site_id"),
        ("instances/site.yaml", lambda d: d.update(extra=1), "unknown"),
        ("services/collector.yaml", lambda d: d.update(unknown_section=1), "unknown"),
    ],
)
def test_unknown_fields_and_illegal_types_rejected(
    config_tree: ConfigTree, relative: str, mutator, match: str
) -> None:
    document = config_tree.read(relative)
    mutator(document)
    config_tree.write(relative, document)
    with pytest.raises(ConfigError, match=match):
        _loader(config_tree).load()


def test_unsupported_schema_version_rejected(config_tree: ConfigTree) -> None:
    site = config_tree.read("instances/site.yaml")
    site["schema_version"] = 2
    config_tree.write("instances/site.yaml", site)
    with pytest.raises(ConfigError, match="schema_version"):
        _loader(config_tree).load()


def test_duplicate_yaml_keys_rejected(config_tree: ConfigTree) -> None:
    target = config_tree.devices_yaml
    target.write_text(
        "schema_version: 1\ndevices:\n  WT01: {}\n  WT01: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate YAML key"):
        _loader(config_tree).load()


def _point_table(
    config_tree: ConfigTree, version: str, *, parent: str | None, protocol: str = "modbus"
):
    doc = config_tree.read("templates/point_tables/modbus_wtg/1.0.0.yaml")
    doc["template_version"] = version
    doc["protocol"] = protocol
    if parent is None:
        doc.pop("parent", None)
    else:
        doc["parent"] = {"id": "modbus_wtg", "version": parent}
    config_tree.write(f"templates/point_tables/modbus_wtg/{version}.yaml", doc)


def test_point_table_inheritance_chain_resolves(config_tree: ConfigTree) -> None:
    # 2.0.0 继承 1.0.0（同协议）：链解析成功。
    _point_table(config_tree, "2.0.0", parent="1.0.0")
    model = config_tree.read("templates/device_models/modbus_wtg/1.0.0.yaml")
    model["point_table"]["version"] = "2.0.0"
    config_tree.write("templates/device_models/modbus_wtg/1.0.0.yaml", model)
    loaded = _loader(config_tree).load()
    inherited = loaded.devices["WT01"].inherited_tables
    assert [item.reference.template_version for item in inherited] == ["1.0.0"]


def test_point_table_inheritance_cycle_rejected(config_tree: ConfigTree) -> None:
    # 1.0.0 → 2.0.0 → 1.0.0 构成环。
    _point_table(config_tree, "2.0.0", parent="1.0.0")
    _point_table(config_tree, "1.0.0", parent="2.0.0")
    with pytest.raises(ConfigError, match="cycle|inheritance"):
        _loader(config_tree).load()


def _point_model_at(config_tree: ConfigTree, version: str) -> None:
    model = config_tree.read("templates/device_models/modbus_wtg/1.0.0.yaml")
    model["point_table"]["version"] = version
    config_tree.write("templates/device_models/modbus_wtg/1.0.0.yaml", model)


def test_point_table_parent_protocol_mismatch_rejected(config_tree: ConfigTree) -> None:
    _point_table(config_tree, "2.0.0", parent="1.0.0", protocol="ads")
    _point_model_at(config_tree, "2.0.0")
    with pytest.raises(ConfigError, match="protocol mismatch"):
        _loader(config_tree).load()


def test_point_table_parent_missing_reference_rejected(config_tree: ConfigTree) -> None:
    _point_table(config_tree, "2.0.0", parent="9.9.9")
    _point_model_at(config_tree, "2.0.0")
    with pytest.raises(ConfigError, match="not found"):
        _loader(config_tree).load()


def test_point_table_reference_must_be_exact_mapping(config_tree: ConfigTree) -> None:
    doc = config_tree.read("templates/device_models/modbus_wtg/1.0.0.yaml")
    doc["protocol"] = {"id": "modbus_tcp"}  # 缺 version
    config_tree.write("templates/device_models/modbus_wtg/1.0.0.yaml", doc)
    with pytest.raises(ConfigError, match="reference"):
        _loader(config_tree).load()


def test_config_snapshot_domain_mapping(config_tree: ConfigTree) -> None:
    from core.infrastructure.config.collector_snapshot import build_collector_snapshot

    snapshot = build_collector_snapshot(_loader(config_tree).load())
    assert set(snapshot.site.devices) == {"WT01"}
    table = next(iter(snapshot.point_tables.values()))
    assert "active_power" in table.points
    assert snapshot.site.site_id == "example_site"
