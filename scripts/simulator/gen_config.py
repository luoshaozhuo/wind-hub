#!/usr/bin/env python
"""生成 ``configs/simulation/`` —— 与模拟器 40 台设备一一对应的真实
Core 配置树（site/device_models/devices/points/business_points/tasks/sinks/
system）。

配置是**静态提交**的（本脚本只在拓扑变化时重跑）；Collector / Commander /
Web Server 直接以 ``--config configs/simulation`` 启动即可连上模拟器。

与模拟器设备布局的对应关系（见 scripts/simulator/devices.py）：

- Modbus：holding register，float32 big-endian 双寄存器 + int16 单寄存器，
  ``word_order: big_endian``，unit_id 1；
- ADS：符号 ``MAIN.<point>``（REAL / DINT），read_mode sum，每设备独立
  loopback IP + target_net_id ``<ip>.1.1``；
- IEC104：M_ME_NC_1 遥测 IOA 1001+，控制点 C_SE_NC_1 IOA 2001 /
  C_SC_NA_1 IOA 2002。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.simulator.devices import ADS_TCP_PORT, IEC104_CONTROL_IOA, MODBUS_LAYOUT  # noqa: E402
from scripts.simulator.generators import (  # noqa: E402
    CONTROL_FLOAT_POINTS,
    CONTROL_INT_POINTS,
    TELEMETRY_FLOAT_POINTS,
    TELEMETRY_INT_POINTS,
)
from scripts.simulator.main import (  # noqa: E402
    ADS_BASE_HOST,
    ADS_COUNT,
    IEC104_BASE_PORT,
    IEC104_COUNT,
    MODBUS_BASE_PORT,
    MODBUS_COUNT,
)

OUT_DIR = REPO_ROOT / "configs" / "simulation"

_FLOAT_UNITS = {
    "active_power": "kilowatt",
    "wind_speed": "meter_per_second",
    "rotor_speed": "rpm",
    "generator_temp": "celsius",
    "pitch_angle": "degree",
    "power_setpoint": "kilowatt",
}


def _dump(name: str, data: dict[str, object]) -> None:
    header = (
        f"# configs/simulation/{name}.yaml — 由 scripts/simulator/gen_config.py 生成\n"
        "# 与 scripts/simulator（40 台模拟 PLC）一一对应；拓扑变化时重跑生成脚本，\n"
        "# 不要手工偏离模拟器布局。\n"
    )
    (OUT_DIR / f"{name}.yaml").write_text(
        header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _system() -> dict[str, object]:
    return {
        "site": {"site_id": "simulation", "name": "模拟风场（40 台模拟 PLC）"},
        "runtime": {
            "queue_maxsize": 10000,
            "backpressure_policy": "drop_old",
            "shutdown_timeout": 10.0,
            "connect_timeout": 5.0,
            "read_timeout": 3.0,
            "watchdog_interval": 5.0,
            "health_timeout": 1.0,
            "write_timeout": 3.0,
        },
        "ads": {
            "local_ams_net_id": "127.0.0.1.1.1",
            "local_ip": "127.0.0.1",
        },
        "interfaces": {"api": {"enabled": True, "host": "127.0.0.1", "port": 8080}},
    }


def _business_points() -> dict[str, object]:
    points: dict[str, object] = {}
    for name in (*TELEMETRY_FLOAT_POINTS, *CONTROL_FLOAT_POINTS):
        points[name] = {"data_type": "float32", "unit": _FLOAT_UNITS[name]}
    for name in (*TELEMETRY_INT_POINTS, *CONTROL_INT_POINTS):
        points[name] = {"data_type": "int16", "unit": "none"}
    return {"business_points": points}


def _point_entry(point_id: str, groups: list[str], address: dict[str, object]) -> dict[str, object]:
    floats = (*TELEMETRY_FLOAT_POINTS, *CONTROL_FLOAT_POINTS)
    data_type = "float32" if point_id in floats else "int16"
    return {
        "point_id": point_id,
        "business_point_id": point_id,
        "variable_name": point_id,
        "point_groups": groups,
        "address": address,
        "data_type": data_type,
        "scale": 1.0,
        "offset": 0.0,
        "unit": _FLOAT_UNITS.get(point_id, "none"),
    }


def _points() -> dict[str, object]:
    telemetry = [*TELEMETRY_FLOAT_POINTS, *TELEMETRY_INT_POINTS]
    control = [*CONTROL_FLOAT_POINTS, *CONTROL_INT_POINTS]

    modbus_points = []
    for name in telemetry:
        addr, _count, _is_float = MODBUS_LAYOUT[name]
        modbus_points.append(_point_entry(name, ["all"], {"type": "holding", "address": addr}))
    for name in control:
        addr, _count, _is_float = MODBUS_LAYOUT[name]
        modbus_points.append(
            _point_entry(name, ["all", "control"], {"type": "holding", "address": addr})
        )

    ads_points = [
        _point_entry(name, ["all", "control"] if name in control else ["all"],
                     {"symbol": f"MAIN.{name}"})
        for name in (*telemetry, *control)
    ]

    iec104_points = []
    ioa = 1001
    for name in TELEMETRY_FLOAT_POINTS:
        iec104_points.append(
            _point_entry(name, ["all"], {"type": "measured_value", "ioa": ioa})
        )
        ioa += 1
    iec104_points.append(
        _point_entry(
            "power_setpoint",
            ["all", "control"],
            {"type": "setpoint_command", "ioa": IEC104_CONTROL_IOA["power_setpoint"]},
        )
    )
    iec104_points.append(
        _point_entry(
            "turbine_command",
            ["all", "control"],
            {"type": "single_command", "ioa": IEC104_CONTROL_IOA["turbine_command"]},
        )
    )

    return {
        "point_tables": {
            "sim_modbus_v1": {"protocol": "modbus", "points": modbus_points},
            "sim_ads_v1": {"protocol": "ads", "points": ads_points},
            "sim_iec104_v1": {"protocol": "iec104", "points": iec104_points},
        }
    }


def _device_models() -> dict[str, object]:
    return {
        "device_types": {"turbine": {"name": "风力发电机组"}},
        "device_models": {
            "sim_modbus_wtg": {
                "device_type": "turbine",
                "protocol": "modbus",
                "point_table": "sim_modbus_v1",
                "connection_defaults": {
                    "unit_id": 1,
                    "timeout": 3.0,
                    "word_order": "big_endian",
                },
            },
            "sim_ads_wtg": {
                "device_type": "turbine",
                "protocol": "ads",
                "point_table": "sim_ads_v1",
                "connection_defaults": {
                    "port": 801,
                    "twincat_version": "2",
                    "timeout": 3.0,
                },
            },
            "sim_iec104_wtg": {
                "device_type": "turbine",
                "protocol": "iec104",
                "point_table": "sim_iec104_v1",
                "connection_defaults": {"port": 2404},
            },
        },
    }


def _devices() -> dict[str, object]:
    devices: list[dict[str, object]] = []
    for i in range(MODBUS_COUNT):
        devices.append(
            {
                "device_id": f"sim-mb-{i + 1:02d}",
                "model": "sim_modbus_wtg",
                "device_group": "turbine_modbus",
                "enabled": True,
                "endpoint": {"host": "127.0.0.1", "port": MODBUS_BASE_PORT + i},
            }
        )
    for i in range(IEC104_COUNT):
        devices.append(
            {
                "device_id": f"sim-i104-{i + 1:02d}",
                "model": "sim_iec104_wtg",
                "device_group": "turbine_iec104",
                "enabled": True,
                "endpoint": {"host": "127.0.0.1", "port": IEC104_BASE_PORT + i},
            }
        )
    for i in range(ADS_COUNT):
        host = f"127.0.0.{ADS_BASE_HOST + 1 + i}"
        devices.append(
            {
                "device_id": f"sim-ads-{i + 1:02d}",
                "model": "sim_ads_wtg",
                "device_group": "turbine_ads",
                "enabled": True,
                "endpoint": {
                    "host": host,
                    "port": ADS_TCP_PORT,
                    "extensions": {"target_net_id": f"{host}.1.1"},
                },
            }
        )
    return {"devices": devices}


def _tasks() -> dict[str, object]:
    return {
        "tasks": [
            {
                "task_id": "sim-modbus-all",
                "device_group": "turbine_modbus",
                "point_group": "all",
                "interval": 1.0,
                "targets": [{"sink": "file_archive"}],
                "enabled": True,
            },
            {
                "task_id": "sim-iec104-all",
                "device_group": "turbine_iec104",
                "point_group": "all",
                "interval": 1.0,
                "targets": [{"sink": "file_archive"}],
                "enabled": True,
            },
            {
                "task_id": "sim-ads-all",
                "device_group": "turbine_ads",
                "point_group": "all",
                "interval": 1.0,
                "targets": [{"sink": "file_archive"}],
                "enabled": True,
            },
        ]
    }


def _sinks() -> dict[str, object]:
    return {
        "sinks": [
            {
                "name": "file_archive",
                "type": "file",
                "enabled": True,
                "connection": {
                    "path": "/var/tmp/wind-hub/simulation/archive.csv",
                    "max_size_mb": 100,
                },
                "points": [],
            },
            {
                "name": "redis_cache",
                "type": "redis",
                "enabled": False,
                "connection": {"host": "127.0.0.1", "port": 6379},
                "points": [],
            },
            {
                "name": "modbus_scada",
                "type": "modbus",
                "enabled": False,
                "connection": {"host": "0.0.0.0", "port": 1502},
                "points": [],
            },
        ]
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _dump("system", _system())
    _dump("business_points", _business_points())
    _dump("points", _points())
    _dump("device_models", _device_models())
    _dump("devices", _devices())
    _dump("tasks", _tasks())
    _dump("sinks", _sinks())
    print(f"已生成 {OUT_DIR}（7 个配置文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
