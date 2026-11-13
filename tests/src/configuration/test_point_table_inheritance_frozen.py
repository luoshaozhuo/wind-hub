"""S2-10：点表继承、覆盖优先级、版本精确引用与错误路径的冻结回归。

仓库真实 configs 未使用 ``parent``/``remove_points``，真实资产加载用例覆盖
不到这两条路径，因此在临时配置树内构造版本化模板，分别从两个层面验证：

1. configuration 层：``validate_point_table_chain`` / ``resolve_device_templates``
   解析出的父链顺序正确，且父/子模板文档全部经深层冻结并脱离磁盘文件；
   继承环、协议不一致、父版本精确引用失败走 ConfigError 错误路径。
2. Industrial Gateway 层：``load_gateway`` 在冻结文档上完成
   “继承 → remove_points → 子表覆盖”合并，覆盖优先级与移除语义不回归。
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml

from core.application.errors import ConfigError
from core.configuration.template_graph import resolve_device_templates
from core.configuration.versioned_templates import TemplateRef, VersionedTemplateRepository
from industrial_gateway.infrastructure.config.resolution import load_gateway

pytestmark = [pytest.mark.component, pytest.mark.contract]


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _protocol(root: Path) -> None:
    _dump(
        root / "templates" / "protocols" / "modbus_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "modbus",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "protocol_options": {
                "mode": "tcp",
                "slave_id": 1,
                "timeout": 5.0,
                "word_order": "big_endian",
            },
        },
    )


def _model(root: Path, *, table: str = "child", table_version: str = "1.0.0") -> None:
    _dump(
        root / "templates" / "device_models" / "turbine_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "turbine",
            "file_version": "1.0.0",
            "protocol": {"id": "modbus", "version": "1.0.0"},
            "point_table": {"id": table, "version": table_version},
        },
    )


def _inheritance_tree(root: Path) -> None:
    """base 定义 p1/p2/p3；child 继承 base，覆盖 p2、新增 p4、移除 p3。"""
    _protocol(root)
    _dump(
        root / "templates" / "point_tables" / "base_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "base",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "points": {
                "p1": {
                    "data_type": "float32",
                    "address": {
                        "register_type": "holding",
                        "address": 100,
                        "word_order": "big_endian",
                    },
                },
                "p2": {
                    "data_type": "uint16",
                    "address": {"register_type": "holding", "address": 101},
                },
                "p3": {"data_type": "bool", "address": {"register_type": "coil", "address": 0}},
            },
        },
    )
    _dump(
        root / "templates" / "point_tables" / "child_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "child",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "parent": {"id": "base", "version": "1.0.0"},
            "remove_points": ["p3"],
            "points": {
                "p2": {
                    "data_type": "uint16",
                    "address": {"register_type": "holding", "address": 200},
                },
                "p4": {
                    "data_type": "int16",
                    "address": {"register_type": "input", "address": 365},
                },
            },
        },
    )
    _model(root)


def test_point_table_chain_resolves_parent_and_freezes_documents(tmp_path: Path) -> None:
    """父链按序解析，父/子文档均深层冻结；磁盘后续变更不影响已解析快照。"""
    _inheritance_tree(tmp_path)
    repository = VersionedTemplateRepository(tmp_path / "templates")
    model = repository.resolve(TemplateRef("device_models", "turbine", "1.0.0"))

    resolved = resolve_device_templates(repository, model)

    assert [table.reference.template_id for table in resolved.inherited_tables] == ["base"]
    child = resolved.point_table
    assert child.reference.template_id == "child"
    for table in (child, *resolved.inherited_tables, resolved.protocol, resolved.model):
        assert isinstance(table.document, MappingProxyType)
    assert isinstance(child.document["points"], MappingProxyType)
    with pytest.raises(TypeError):
        child.document["points"]["p2"] = {}  # type: ignore[index]

    # 脱离磁盘：覆写 YAML 后已解析模板保持旧内容，重新解析才看到新内容。
    child_path = tmp_path / "templates" / "point_tables" / "child_v1.0.0.yaml"
    rewritten = yaml.safe_load(child_path.read_text(encoding="utf-8"))
    rewritten["points"]["p2"]["address"]["address"] = 999
    _dump(child_path, rewritten)

    assert resolved.point_table.document["points"]["p2"]["address"]["address"] == 200
    reloaded = VersionedTemplateRepository(tmp_path / "templates").resolve(
        TemplateRef("point_tables", "child", "1.0.0")
    )
    assert reloaded.document["points"]["p2"]["address"]["address"] == 999


def test_chain_rejects_inheritance_cycle(tmp_path: Path) -> None:
    """继承环是显式错误路径，不得死循环。"""
    _protocol(root=tmp_path)
    _dump(
        tmp_path / "templates" / "point_tables" / "loop_a_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "loop_a",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "parent": {"id": "loop_b", "version": "1.0.0"},
            "points": {},
        },
    )
    _dump(
        tmp_path / "templates" / "point_tables" / "loop_b_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "loop_b",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "parent": {"id": "loop_a", "version": "1.0.0"},
            "points": {},
        },
    )
    _model(tmp_path, table="loop_a")
    repository = VersionedTemplateRepository(tmp_path / "templates")
    model = repository.resolve(TemplateRef("device_models", "turbine", "1.0.0"))

    with pytest.raises(ConfigError, match="inheritance cycle"):
        resolve_device_templates(repository, model)


def test_chain_rejects_parent_protocol_mismatch(tmp_path: Path) -> None:
    """父表协议与根表不一致走 ConfigError，防止跨协议继承。"""
    _protocol(tmp_path)
    _dump(
        tmp_path / "templates" / "point_tables" / "ads_base_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "ads_base",
            "file_version": "1.0.0",
            "protocol": "ads",
            "points": {},
        },
    )
    _dump(
        tmp_path / "templates" / "point_tables" / "modbus_child_v1.0.0.yaml",
        {
            "schema_version": 1,
            "template_id": "modbus_child",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "parent": {"id": "ads_base", "version": "1.0.0"},
            "points": {},
        },
    )
    _model(tmp_path, table="modbus_child")
    repository = VersionedTemplateRepository(tmp_path / "templates")
    model = repository.resolve(TemplateRef("device_models", "turbine", "1.0.0"))

    with pytest.raises(ConfigError, match="protocol mismatch"):
        resolve_device_templates(repository, model)


def test_chain_requires_exact_parent_version(tmp_path: Path) -> None:
    """父引用必须精确命中已发布版本，缺失版本按错误路径拒绝而非静默回退。"""
    _inheritance_tree(tmp_path)
    child_path = tmp_path / "templates" / "point_tables" / "child_v1.0.0.yaml"
    document = yaml.safe_load(child_path.read_text(encoding="utf-8"))
    document["parent"] = {"id": "base", "version": "9.9.9"}
    _dump(child_path, document)
    repository = VersionedTemplateRepository(tmp_path / "templates")
    model = repository.resolve(TemplateRef("device_models", "turbine", "1.0.0"))

    with pytest.raises(ConfigError):
        resolve_device_templates(repository, model)


def test_gateway_merges_inheritance_override_and_removal_on_frozen_docs(tmp_path: Path) -> None:
    """Gateway 在冻结模板上执行合并：继承保留、remove_points 生效、子表覆盖优先。"""
    _inheritance_tree(tmp_path)
    _dump(
        tmp_path / "templates" / "units.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "quantities": {"dimensionless": {"base_unit": "1"}},
            "units": {"1": {"symbol": "1", "quantity": "dimensionless"}},
        },
    )
    _dump(
        tmp_path / "templates" / "business_points.yaml",
        {"schema_version": 1, "file_version": "1.0.0", "business_points": {}},
    )
    _dump(
        tmp_path / "instances" / "devices.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "devices": {
                "WT01": {
                    "name": "继承测试机组",
                    "enabled": True,
                    "model": {"id": "turbine", "version": "1.0.0"},
                    "endpoint": {"host": "127.0.0.1", "port": 1502},
                    "overrides": {"protocol_options": {"slave_id": 1}},
                }
            },
        },
    )
    service_path = tmp_path / "service.yaml"
    _dump(
        service_path,
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "config_sources": {
                "instances": {"devices": "instances/devices.yaml"},
                "templates": {"root": "templates"},
            },
            "grpc": {"listen_host": "127.0.0.1", "listen_port": 1504},
            "servers": [
                {
                    "server_id": "gw1",
                    "device_id": "WT01",
                    "listen_host": "127.0.0.1",
                    "listen_port": 1503,
                    "desired_state": "START",
                    "control": {
                        "enabled": True,
                        "target": {
                            "kind": "commander",
                            "endpoint": "127.0.0.1:1505",
                        },
                        "mappings": [
                            {"variable_id": "p1", "target_point_id": "p1"},
                            {"variable_id": "p2", "target_point_id": "p2"},
                        ],
                    },
                }
            ],
            "metrics": {"window_seconds": 60, "collect_interval_ms": 1000},
        },
    )

    gateway = load_gateway(service_path, project_root=tmp_path)
    server = gateway.servers["gw1"]

    # p3 被 remove_points 移除；p1 继承自父表；p4 为子表新增。
    assert set(server.variables) == {"p1", "p2", "p4"}
    # 覆盖优先级：子表 p2 的地址 200 击败父表的 101。
    assert server.variables["p2"].address["address"] == 200
    assert server.variables["p1"].address["address"] == 100
    assert server.variables["p4"].address["address"] == 365
    # 控制映射仅保留控制点，寄存器映射与点表一致。
    assert {mapping.point_id for mapping in server.registers} == {"p1", "p2", "p4"}
