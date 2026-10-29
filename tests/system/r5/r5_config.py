"""Round 5 真实链路测试的配置构造（栈中立，只依赖 config helper）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.support.config_helper import write_config_tree
from tests.support.functional_config import MODBUS_POINTS, modbus_device_dict

TASK_ID = "modbus-telemetry"
DEVICE_ID = "modbus-1"
INSTANCE_ID = f"{TASK_ID}:{DEVICE_ID}"
SITE_ID = "test"


def write_r5_config(
    base: Path,
    modbus_port: int,
    nats_url: str,
    sinks: dict[str, Any],
    *,
    tasks: dict[str, Any] | None = None,
    delivery: str = "realtime",
    max_staleness_ms: int = 60_000,
    interval: float = 0.2,
) -> Path:
    """写出单 Modbus 设备 + 任意 Sink 组合的自包含配置树。"""
    messaging: dict[str, Any] = {
        "backend": "nats",
        "servers": [nats_url],
        "delivery": delivery,
    }
    if delivery == "realtime":
        messaging["max_staleness_ms"] = max_staleness_ms
    return write_config_tree(
        base,
        devices=modbus_device_dict(modbus_port),
        point_tables={"modbus": {"points": dict(MODBUS_POINTS)}},
        sinks=sinks,
        tasks=tasks
        if tasks is not None
        else {
            TASK_ID: {
                "device_group_id": DEVICE_ID,
                "point_group": "telemetry",
                "interval": interval,
            }
        },
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
                "watchdog_interval": 0.5,
                "health_timeout": 0.5,
            },
            "messaging": messaging,
        },
    )
