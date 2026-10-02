"""Commander component 测试共享装配。

在进程内用真实 ``assemble_commander`` 装配 CommanderApp（真实配置加载、
真实 generation 构建），不做网络 I/O；设备连接语义由 system/command 层
覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.config_helper import write_config_tree
from wind_hub_commander.assembly import CommanderApp, assemble_commander

#: 最小 Modbus 设备与点表——不连接，只提供 generation 构建素材。
_POINTS: list[dict[str, object]] = [
    {
        "point_id": "setpoint.power",
        "point_groups": ["control"],
        "address": {"register_type": "holding", "address": 200},
        "data_type": "float32",
    },
    {
        "point_id": "rotor.speed",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 100},
        "data_type": "float32",
    },
]


def write_commander_config(base: Path, *, port: int = 15020) -> Path:
    """写出单 Modbus 设备的最小 Commander 配置集（不依赖网络可达性）。"""
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "modbus-1",
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": port,
                    "extensions": {"unit_id": 1, "timeout": 1.0},
                },
            }
        ],
        point_tables={"modbus": {"protocol": "modbus", "points": list(_POINTS)}},
        tasks=[],
        sinks=[],
        system={"runtime": {"connect_timeout": 1.0, "write_timeout": 1.0}},
    )


@pytest.fixture
def commander_app(tmp_path: Path) -> CommanderApp:
    """装配好的 CommanderApp（未连接任何设备）。"""
    return assemble_commander(write_commander_config(tmp_path / "cfg"))
