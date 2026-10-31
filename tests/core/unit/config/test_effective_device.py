"""运行时有效连接参数合成：端口三级回退与协议选项合并。"""

from __future__ import annotations

from core.domain import (
    ConnectionEndpoint,
    Device,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)
from core.infrastructure.config.effective import resolve_effective_device


def _model(
    *,
    endpoint_defaults: dict[str, object] | None = None,
    protocol_options: dict[str, object] | None = None,
) -> DeviceModel:
    return DeviceModel(
        device_model_id=DeviceModelId("wtg@1.0.0"),
        device_type_id=DeviceTypeId("wtg"),
        point_table_id=PointTableId("wtg@1.0.0"),
        endpoint_defaults=endpoint_defaults or {},
        protocol_options=protocol_options or {},
    )


def _device(
    *,
    port: int | None,
    protocol_options: dict[str, object] | None = None,
) -> Device:
    return Device(
        device_id=DeviceId("WT01"),
        device_model_id=DeviceModelId("wtg@1.0.0"),
        endpoint=ConnectionEndpoint(host="192.0.2.10", port=port),
        protocol_options=protocol_options or {},
        device_group_ids=(DeviceGroupId("WT01"),),
    )


def test_modbus_port_falls_back_to_protocol_default() -> None:
    resolved, options = resolve_effective_device(_device(port=None), _model(), "modbus")
    assert resolved.endpoint.port == 502
    # 选项为空时不应往设备上回填协议代码默认值。
    assert dict(options) == {}
    assert dict(resolved.protocol_options) == {}


def test_model_endpoint_default_port_beats_protocol_default() -> None:
    model = _model(endpoint_defaults={"port": 1502})
    resolved, _ = resolve_effective_device(_device(port=None), model, "modbus")
    assert resolved.endpoint.port == 1502


def test_explicit_device_port_beats_model_default() -> None:
    model = _model(endpoint_defaults={"port": 1502})
    resolved, _ = resolve_effective_device(_device(port=1700), model, "modbus")
    assert resolved.endpoint.port == 1700


def test_device_options_override_model_options() -> None:
    model = _model(protocol_options={"slave_id": 1, "timeout": 4.0})
    device = _device(port=1502, protocol_options={"slave_id": 10})
    resolved, options = resolve_effective_device(device, model, "modbus")
    assert dict(options) == {"slave_id": 10, "timeout": 4.0}
    assert dict(resolved.protocol_options) == {"slave_id": 10, "timeout": 4.0}


def test_ads_default_port_follows_twincat_version() -> None:
    tc2, _ = resolve_effective_device(_device(port=None), _model(), "ads")
    assert tc2.endpoint.port == 801

    tc3_model = _model(protocol_options={"twincat_version": "3"})
    tc3, _ = resolve_effective_device(_device(port=None), tc3_model, "ads")
    assert tc3.endpoint.port == 851


def test_iec104_port_falls_back_to_protocol_default() -> None:
    resolved, _ = resolve_effective_device(_device(port=None), _model(), "iec104")
    assert resolved.endpoint.port == 2404
