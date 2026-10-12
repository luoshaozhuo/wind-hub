"""Functional/system 测试共享的功能配置写出辅助（栈中立）。

从原 ``tests/component/collector/conftest.py`` 迁出：仅包含自包含配置
目录的写出与改写能力，不依赖任何协议栈实现（新旧 Collector/Commander
进程均可消费写出的配置树）。

运行时通信恢复由 ``system.yaml`` 的 ``runtime.watchdog_interval`` /
``runtime.health_timeout`` 控制；协议读写本身不执行自动重试。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from tests.support.config_helper import write_config_tree

#: 与 ModbusMockServer 默认寄存器布局一致的点表。
MODBUS_POINTS: dict[str, dict[str, Any]] = {
    "rotor.speed": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 100},
        "data_type": "float32",
    },
    "gen.power": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 102},
        "data_type": "float32",
        # 工程值换算验证点：raw 800.0 → 800*2+10 = 1610.0。
        "scale": 2.0,
        "offset": 10.0,
    },
    "temp.int": {
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 104},
        "data_type": "int16",
    },
    "setpoint.power": {
        "point_groups": ["telemetry", "control"],
        "address": {"register_type": "holding", "address": 200},
        "data_type": "float32",
    },
}

DEFAULT_TASK: dict[str, Any] = {
    "device_group_id": "modbus-1",
    "point_group": "telemetry",
    "interval": 0.2,
    "sink_ids": ["null_sink"],
}


def modbus_device_dict(port: int, **overrides: Any) -> dict[str, Any]:
    """指向指定端口 Modbus fixture 的设备条目（devices Mapping 单条目）。"""
    device: dict[str, Any] = {
        "modbus-1": {
            "protocol": "modbus",
            "point_table": "modbus",
            "endpoint": {
                "host": "127.0.0.1",
                "port": port,
            },
            "protocol_options": {
                "unit_id": 1,
                "timeout": 2.0,
                # fixture server 寄存器布局为 big-endian float32。
                "word_order": "big_endian",
            },
        }
    }
    if overrides:
        device["modbus-1"].update(overrides)
    return device


#: devices 参数允许传 callable：在工厂选定实际端口后再构造设备 Mapping。
DevicesArg = dict[str, Any] | Callable[[int], dict[str, Any]]


def write_functional_config(
    base: Path,
    port: int,
    *,
    devices: DevicesArg | None = None,
    point_tables: dict[str, Any] | None = None,
    tasks: dict[str, Any] | None = None,
    sinks: dict[str, Any] | None = None,
    system: dict[str, Any] | None = None,
    device_models: dict[str, Any] | None = None,
) -> Path:
    """写出 functional 测试默认配置树（单 Modbus 设备 + null sink）。"""
    if callable(devices):
        devices = devices(port)
    return write_config_tree(
        base,
        devices=devices if devices is not None else modbus_device_dict(port),
        point_tables=point_tables or {"modbus": {"points": dict(MODBUS_POINTS)}},
        device_models=device_models,
        sinks=sinks
        if sinks is not None
        else {
            "null_sink": {
                "type": "file",
                "connection": {"path": "/tmp/wind-hub-null.csv"},
            }
        },
        tasks=tasks if tasks is not None else {"modbus-telemetry": dict(DEFAULT_TASK)},
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
                "watchdog_interval": 0.5,
                "health_timeout": 0.5,
                **((system or {}).get("runtime") or {}),
            },
            **{k: v for k, v in (system or {}).items() if k != "runtime"},
        },
    )


def update_yaml(config_dir: Path, name: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """读-改-写配置目录中的单个 YAML 文件（reload 测试用）。"""
    path = config_dir / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
