"""Gateway 映射必须匹配逻辑点角色、类型和 Modbus 地址。"""
from pathlib import Path

import pytest

from seahorse.infrastructure.gateway_mapping import validate_gateway_mapping
from seahorse.infrastructure.modbus_publisher import PublishConfig, PublishPoint


def test_gateway_feedback_mapping(tmp_path: Path) -> None:
    config = tmp_path / "gateway.yaml"
    config.write_text(
        "endpoints:\n"
        "  ep: {host: 127.0.0.1, port: 15020}\n"
        "devices:\n"
        "  wt1:\n"
        "    endpoint_id: ep\n"
        "    point_table_id: turbine\n"
        "    protocol_address: {unit_id: 1}\n"
        "point_tables:\n"
        "  turbine:\n"
        "    points:\n"
        "      emsPowAct:\n"
        "        role: feedback\n"
        "        data_type: float32\n"
        "        address: {register_type: holding, address: 100}\n",
        encoding="utf-8",
    )
    point = PublishPoint(
        device_id="wt1", point_id="emsPowAct", host="127.0.0.1",
        port=15020, unit_id=1, address=100
    )
    validate_gateway_mapping(PublishConfig(points=[point]), config)
    with pytest.raises(ValueError, match="mismatch"):
        validate_gateway_mapping(
            PublishConfig(points=[point.model_copy(update={"address": 101})]), config
        )
    with pytest.raises(ValueError, match="feedback"):
        validate_gateway_mapping(
            PublishConfig(points=[point.model_copy(update={"point_id": "ctrlActPow"})]), config
        )
