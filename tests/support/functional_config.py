"""Functional/system 测试共享的功能配置写出辅助（栈中立）。

从原 ``tests/component/collector/conftest.py`` 迁出：仅包含自包含配置
目录的写出与改写能力，不依赖任何协议栈实现（新旧 Collector/Commander
进程均可消费写出的配置树）。

运行时读取重试参数在新栈归属 ``system.yaml`` 的 ``runtime.read_retries`` /
``runtime.retry_interval``，不再作为 Modbus 连接扩展下发。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from tests.support.config_helper import write_config_tree

#: 与 ModbusMockServer 默认寄存器布局一致的点表。
MODBUS_POINTS: list[dict[str, Any]] = [
    {
        "point_id": "rotor.speed",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 100},
        "data_type": "float32",
    },
    {
        "point_id": "gen.power",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 102},
        "data_type": "float32",
        # 工程值换算验证点：raw 800.0 → 800*2+10 = 1610.0。
        "scale": 2.0,
        "offset": 10.0,
    },
    {
        "point_id": "temp.int",
        "point_groups": ["telemetry"],
        "address": {"register_type": "holding", "address": 104},
        "data_type": "int16",
    },
    {
        "point_id": "setpoint.power",
        "point_groups": ["telemetry", "control"],
        "address": {"register_type": "holding", "address": 200},
        "data_type": "float32",
    },
]

DEFAULT_TASK: dict[str, Any] = {
    "task_id": "modbus-telemetry",
    "device": "modbus-1",
    "point_group": "telemetry",
    "interval": 0.2,
    "targets": [{"sink": "null_sink"}],
}


def modbus_device_dict(port: int, **overrides: Any) -> dict[str, Any]:
    """指向指定端口 Modbus fixture 的设备定义（旧式便捷写法）。"""
    device: dict[str, Any] = {
        "device_id": "modbus-1",
        "protocol": "modbus",
        "point_table": "modbus",
        "endpoint": {
            "host": "127.0.0.1",
            "port": port,
            "extensions": {
                "unit_id": 1,
                "timeout": 2.0,
                # fixture server 寄存器布局为 big-endian float32。
                "word_order": "big_endian",
            },
        },
    }
    device.update(overrides)
    return device


#: devices 参数允许传 callable：在工厂选定实际端口后再构造设备列表。
DevicesArg = list[dict[str, Any]] | Callable[[int], list[dict[str, Any]]]


def write_functional_config(
    base: Path,
    port: int,
    *,
    devices: DevicesArg | None = None,
    point_tables: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    sinks: list[dict[str, Any]] | None = None,
    system: dict[str, Any] | None = None,
    device_models: dict[str, Any] | None = None,
) -> Path:
    """写出 functional 测试默认配置树（单 Modbus 设备 + null sink）。"""
    if callable(devices):
        devices = devices(port)
    return write_config_tree(
        base,
        devices=devices if devices is not None else [modbus_device_dict(port)],
        point_tables=point_tables or {"modbus": {"points": list(MODBUS_POINTS)}},
        device_models=device_models,
        sinks=sinks
        if sinks is not None
        else [
            {
                "name": "null_sink",
                "type": "file",
                "connection": {"path": "/tmp/wind-hub-null.jsonl"},
            }
        ],
        tasks=tasks if tasks is not None else [dict(DEFAULT_TASK)],
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
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
