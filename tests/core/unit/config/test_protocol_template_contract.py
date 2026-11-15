"""已发布协议配置模板与 Core 解析器的双向契约检查。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.application.errors import ConfigError
from core.configuration.template_graph import resolve_model_templates
from core.configuration.versioned_templates import TemplateRef, VersionedTemplateRepository
from core.domain import ConnectionEndpoint
from core.infrastructure.config import read_yaml
from core.infrastructure.protocol.ads.config import parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config

pytestmark = [pytest.mark.unit, pytest.mark.safety]

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"
_PROTOCOLS = _CONFIGS / "templates" / "protocols"


# 各协议当前发布版本：与 configs/templates/protocols 的实际版本化文件一致。
_RELEASED_VERSIONS = {"ads": "1.2.0", "modbus_tcp": "1.1.0", "iec104": "1.1.0"}


@pytest.mark.parametrize(
    ("template_id", "protocol_name", "parse"),
    [
        ("ads", "ads", parse_ads_config),
        ("modbus_tcp", "modbus", parse_modbus_config),
        ("iec104", "iec104", parse_iec104_config),
    ],
)
def test_released_protocol_defaults_are_understood_and_applied(
    template_id: str,
    protocol_name: str,
    parse: Callable[..., Any],
) -> None:
    """模板参数不能被静默忽略，解析器默认值与发布模板一致。"""
    version = _RELEASED_VERSIONS[template_id]
    path = _PROTOCOLS / f"protocol_{template_id}_v{version}.yaml"
    document = read_yaml(path)
    assert document["schema_version"] == 1
    assert document["file_version"] == version
    assert document["template_id"] == template_id
    assert document["protocol"] == protocol_name
    options = document["protocol_options"]
    assert isinstance(options, dict) and options

    endpoint = ConnectionEndpoint("127.0.0.1")
    effective = parse(endpoint, options)
    implicit = parse(endpoint, {})
    for name, value in options.items():
        assert hasattr(effective, name), f"{template_id}.{name}: parser did not expose option"
        assert getattr(effective, name) == value, f"{template_id}.{name}: ignored or changed"
        assert getattr(implicit, name) == value, f"{template_id}.{name}: default drift"


@pytest.mark.parametrize("template_id", ["ads", "modbus_tcp", "iec104"])
def test_released_protocol_rejects_unknown_parameter(template_id: str) -> None:
    parsers = {
        "ads": parse_ads_config,
        "modbus_tcp": parse_modbus_config,
        "iec104": parse_iec104_config,
    }
    version = _RELEASED_VERSIONS[template_id]
    document = read_yaml(_PROTOCOLS / f"protocol_{template_id}_v{version}.yaml")
    options = {**document["protocol_options"], "unexpected_option": 1}
    with pytest.raises(ConfigError, match="unknown"):
        parsers[template_id](ConnectionEndpoint("127.0.0.1"), options)


def test_published_device_instances_resolve_all_template_dependencies() -> None:
    """部署中的型号、协议、点表和任务引用均精确指向已发布版本。"""
    devices = read_yaml(_CONFIGS / "instances" / "devices.yaml")["devices"]
    repository = VersionedTemplateRepository(_CONFIGS / "templates")
    assert devices
    for device_id, device in devices.items():
        model = device["model"]
        reference = TemplateRef("device_models", model["id"], model["version"])
        resolved = resolve_model_templates(repository, repository.resolve(reference))
        assert resolved.model.reference == reference, device_id
        assert resolved.protocol.document["protocol"] == resolved.point_table.document["protocol"]
