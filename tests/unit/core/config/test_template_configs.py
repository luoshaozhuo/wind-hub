"""configs 自动验证：加载、引用、往返与协议参数消费一致性。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from collector.infrastructure.config.loader import load_collector_config
from commander.infrastructure.config.loader import load_commander_config
from core.application.config_diff import diff_config_snapshots
from core.domain import ConnectionEndpoint, DeviceId, DeviceModelId
from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.protocol.ads.config import parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config

TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "configs"

_OPTION_PARSERS = {
    "ads": parse_ads_config,
    "modbus": parse_modbus_config,
    "iec104": parse_iec104_config,
}


def test_template_loads_with_valid_references() -> None:
    snapshot = YamlConfigAdapter(TEMPLATE_DIR).load()
    assert len(snapshot.site.devices) == 30
    assert len(snapshot.tasks) == 4
    assert len(snapshot.sinks) == 3
    # 每个机型正好十台设备。
    per_model: dict[str, int] = {}
    for device in snapshot.site.devices.values():
        assert device.device_model_id in snapshot.device_models
        key = str(device.device_model_id)
        per_model[key] = per_model.get(key, 0) + 1
        for group_id in device.device_group_ids:
            assert group_id in snapshot.device_groups
    assert per_model == {"modbus_wtg": 10, "beckhoff_wtg": 10, "iec104_wtg": 10}


def test_template_assembles_collector_and_commander() -> None:
    source = YamlConfigAdapter(TEMPLATE_DIR)
    collector = load_collector_config(source)
    commander = load_commander_config(source)
    # 有效端口：设备显式 > 型号 endpoint_defaults > 协议代码默认。
    ports = {str(key): device.endpoint.port for key, device in collector.devices.items()}
    assert len(ports) == 30
    for index in range(1, 11):
        assert ports[f"modbus-{index:03d}"] == 1502
        assert ports[f"ads-{index:03d}"] == 851
        assert ports[f"iec104-{index:03d}"] == 12404
    assert set(commander.devices) == set(collector.devices)


def test_template_device_overrides_merge_with_model_defaults() -> None:
    """设备显式值按同名 Key 覆盖型号默认值，未覆盖字段继续继承。"""
    snapshot = YamlConfigAdapter(TEMPLATE_DIR).load()
    options = {
        device_id: device.protocol_options
        for device_id, device in snapshot.site.devices.items()
    }
    models = snapshot.device_models

    modbus_override = options[DeviceId("modbus-010")]
    assert modbus_override == {"slave_id": 10}
    merged = {**models[DeviceModelId("modbus_wtg")].protocol_options, **modbus_override}
    config = parse_modbus_config(ConnectionEndpoint(host="127.0.0.20"), merged)
    assert (config.slave_id, config.timeout, config.health_point_id) == (10, 5.0, "active_power")

    ads_override = options[DeviceId("ads-010")]
    assert ads_override == {"target_net_id": "127.0.0.30.1.1", "username": "RouteAdmin"}
    merged_ads = {**models[DeviceModelId("beckhoff_wtg")].protocol_options, **ads_override}
    ads_config = parse_ads_config(ConnectionEndpoint(host="127.0.0.30"), merged_ads)
    assert (ads_config.username, ads_config.twincat_version) == ("RouteAdmin", "3")
    assert ads_config.target_net_id == "127.0.0.30.1.1"

    # 十台 ADS 设备的 target_net_id 互不相同。
    net_ids = {
        options[device_id]["target_net_id"]
        for device_id, device in snapshot.site.devices.items()
        if str(device.device_model_id) == "beckhoff_wtg"
    }
    assert len(net_ids) == 10


def test_template_protocol_options_accepted_by_parsers() -> None:
    snapshot = YamlConfigAdapter(TEMPLATE_DIR).load()
    for device in snapshot.site.devices.values():
        model = snapshot.device_models[device.device_model_id]
        protocol = snapshot.point_tables[model.point_table_id].protocol.name
        endpoint = ConnectionEndpoint(
            host=device.endpoint.host,
            port=device.endpoint.port or model.endpoint_defaults.get("port"),
        )
        options = {
            **model.protocol_options,
            **device.protocol_options,
        }
        _OPTION_PARSERS[protocol](endpoint, options)


def test_template_defaults_match_protocol_code_defaults() -> None:
    """模板文件头声称的协议默认值与解析器代码默认值一致。"""
    endpoint = ConnectionEndpoint(host="192.0.2.1")
    modbus = parse_modbus_config(endpoint, {})
    assert (modbus.port, modbus.slave_id, modbus.timeout, modbus.word_order) == (
        502,
        1,
        5.0,
        "little_endian",
    )
    ads2 = parse_ads_config(endpoint, {})
    assert (ads2.target_port, ads2.timeout, ads2.twincat_version) == (801, 5.0, "2")
    ads3 = parse_ads_config(endpoint, {"twincat_version": "3"})
    assert ads3.target_port == 851
    iec = parse_iec104_config(endpoint, {})
    assert (iec.port, iec.default_ca, iec.k, iec.w) == (2404, 1, 12, 8)
    assert (iec.t0, iec.t1, iec.t2, iec.t3) == (30.0, 15.0, 10.0, 20.0)


def test_template_save_load_round_trip(tmp_path: Path) -> None:
    for source_file in TEMPLATE_DIR.glob("*.yaml"):
        shutil.copy2(source_file, tmp_path)
    adapter = YamlConfigAdapter(tmp_path)
    first = adapter.load()
    adapter.save(first)
    second = adapter.load()
    assert not diff_config_snapshots(first, second).has_changes
    # 再次保存必须与首次输出字节一致：不允许逐次保存产生配置漂移。
    first_dump = {
        path.name: path.read_text(encoding="utf-8") for path in tmp_path.glob("*.yaml")
    }
    adapter.save(second)
    third = adapter.load()
    assert not diff_config_snapshots(second, third).has_changes
    second_dump = {
        path.name: path.read_text(encoding="utf-8") for path in tmp_path.glob("*.yaml")
    }
    assert first_dump == second_dump


@pytest.mark.parametrize("filename", ["devices.yaml", "tasks.yaml", "sinks.yaml", "points.yaml"])
def test_template_entity_collections_are_mappings(filename: str) -> None:
    import yaml

    document = yaml.safe_load((TEMPLATE_DIR / filename).read_text(encoding="utf-8"))
    root = next(iter(document.values()))
    assert isinstance(root, dict) or filename == "points.yaml"


def test_ads_point_types_use_plc_native_names() -> None:
    snapshot = YamlConfigAdapter(TEMPLATE_DIR).load()
    for table in snapshot.point_tables.values():
        if table.protocol.name != "ads":
            continue
        for point in table.points.values():
            assert point.ext["data_type"] in {
                "BOOL", "SINT", "USINT", "INT", "UINT",
                "DINT", "UDINT", "LINT", "ULINT",
                "REAL", "LREAL", "STRING",
            }


def test_configs_are_flat_without_legacy_samples() -> None:
    assert (TEMPLATE_DIR / "points.yaml").is_file()
    assert not (TEMPLATE_DIR / "template").exists()
    assert not (TEMPLATE_DIR / "example_ads").exists()
    assert not (TEMPLATE_DIR / "example_modbus").exists()
