"""从正式 Core 示例生成可直接启动的三协议虚拟风场配置。

生成的文件是完整的 site 配置，不依赖其他 tests fixture；
scripts/protocol_servers.py --config <output> 会逐设备按此文件绑定并动态出数。
默认每协议十台，ADS 复用 TCP 48898 但占用十个不同 loopback IP。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import yaml

from core.infrastructure.config import YamlConfigAdapter

_REPO_ROOT = Path(__file__).resolve().parent.parent
#: 完整三协议风场以测试夹具中的 Modbus 示例站点为种子（configs/ 下的
#: example_modbus 已随配置 Schema 整改移除，单文件出厂配置不适合整目录复制）。
_TEMPLATE = _REPO_ROOT / "tests" / "fixtures" / "configs" / "example_modbus"


def _read(directory: Path, filename: str) -> dict[str, object]:
    with (directory / filename).open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError(f"{filename}: expected mapping document")
    return raw


def _write(directory: Path, filename: str, document: dict[str, object]) -> None:
    with (directory / filename).open("w", encoding="utf-8") as stream:
        yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)


def generate_site(
    output: Path,
    *,
    count: int = 10,
    modbus_base_port: int = 15020,
    iec104_base_port: int = 15420,
    ads_host_prefix: str = "127.0.1",
) -> Path:
    """生成 3×count 台独立监听设备，严禁覆盖已存在的配置。"""
    if not 1 <= count <= 200:
        raise ValueError("device count per protocol must be 1..200")
    if not 1024 <= modbus_base_port <= 65535 - count:
        raise ValueError("invalid Modbus port range")
    if not 1024 <= iec104_base_port <= 65535 - count:
        raise ValueError("invalid IEC104 port range")
    modbus_ports = set(range(modbus_base_port, modbus_base_port + count))
    iec104_ports = set(range(iec104_base_port, iec104_base_port + count))
    if modbus_ports & iec104_ports:
        raise ValueError("Modbus and IEC104 port ranges overlap")
    if output.exists():
        raise FileExistsError(f"destination already exists: {output}")

    shutil.copytree(_TEMPLATE, output)
    try:
        system = _read(output, "system.yaml")
        system.pop("interfaces", None)
        system["site"] = {
            "site_id": "virtual_windfarm",
            "name": "Virtual Wind Farm",
        }
        system["ads"] = {
            "local_ip": "127.0.0.1",
            "local_ams_net_id": "127.0.0.1.1.1",
        }
        _write(output, "system.yaml", system)

        business = _read(output, "business_points.yaml")
        business["business_points"]["e2e_float"] = {
            "data_type": "float32", "unit": "none",
        }
        _write(output, "business_points.yaml", business)

        points = _read(output, "points.yaml")
        # Mapping Key 身份：点表 points 的键即 BusinessPoint ID，
        # 不再有 business_point_id 字段。
        float_point = {
            "unit": "none",
            "point_groups": ["all"],
        }
        # ADS 点表写 PLC 原生类型名（REAL→float32 逻辑类型）；
        # 其余协议直接写逻辑数据类型。
        points["point_tables"]["e2e_ads"] = {
            "protocol": "ads",
            "points": {
                "e2e_float": {
                    **float_point,
                    "data_type": "REAL",
                    "address": {"symbol": "MAIN.var0"},
                }
            },
        }
        points["point_tables"]["e2e_iec104"] = {
            "protocol": "iec104",
            "points": {
                "e2e_float": {
                    **float_point,
                    "data_type": "float32",
                    "address": {"ca": 1, "ioa": 100, "type_id": "M_ME_NC_1"},
                }
            },
        }
        _write(output, "points.yaml", points)

        models = _read(output, "device_models.yaml")
        models["device_models"]["e2e_ads"] = {
            "device_type_id": "turbine",
            "point_table_id": "e2e_ads",
            "protocol": "ads",
            "endpoint_defaults": {"port": 801},
            "protocol_options": {"twincat_version": "2"},
        }
        models["device_models"]["e2e_iec104"] = {
            "device_type_id": "turbine",
            "point_table_id": "e2e_iec104",
            "protocol": "iec104",
            "endpoint_defaults": {"port": 2404},
        }
        _write(output, "device_models.yaml", models)

        groups = {f"virtual_{name}": {"name": f"virtual_{name}"}
                  for name in ("modbus", "ads", "iec104")}
        devices: dict[str, dict[str, object]] = {}
        for index in range(count):
            mb_id = f"modbus-{index:02d}"
            devices[mb_id] = {
                "model_id": "modbus_wtg",
                "device_group_ids": ["virtual_modbus"],
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": modbus_base_port + index,
                },
                "enabled": True,
            }

            octet = index + 11
            if octet > 254:
                raise ValueError("ADS address octet overflows loopback subnet")
            ads_host = f"{ads_host_prefix}.{octet}"
            ads_id = f"ads-{index:02d}"
            devices[ads_id] = {
                "model_id": "e2e_ads",
                "device_group_ids": ["virtual_ads"],
                "endpoint": {"host": ads_host, "port": 801},
                "protocol_options": {"target_net_id": f"{ads_host}.2.1"},
                "enabled": True,
            }

            iec_id = f"iec104-{index:02d}"
            devices[iec_id] = {
                "model_id": "e2e_iec104",
                "device_group_ids": ["virtual_iec104"],
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": iec104_base_port + index,
                },
                "enabled": True,
            }
        _write(output, "devices.yaml", {
            "devices": devices,
            "device_groups": groups,
        })

        tasks = {
            f"virtual-{family}": {
                "device_group_id": f"virtual_{family}",
                "point_group": "all",
                "interval": 1.0,
                "outputs": [{"sink_id": "redis_latest"}],
                "enabled": True,
            }
            for family in ("modbus", "ads", "iec104")
        }
        _write(output, "tasks.yaml", {"tasks": tasks})

        # 拒绝留下不可由 Core 解析的半成品，和 Web Server/Collector 共用契约。
        snapshot = YamlConfigAdapter(output).load()
        if len(snapshot.site.devices) != 3 * count:
            raise RuntimeError("virtual site device count differs from generated config")
        return output
    except BaseException:
        shutil.rmtree(output)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--modbus-base-port", type=int, default=15020)
    parser.add_argument("--iec104-base-port", type=int, default=15420)
    parser.add_argument("--ads-host-prefix", default="127.0.1")
    args = parser.parse_args()
    result = generate_site(
        args.output,
        count=args.count,
        modbus_base_port=args.modbus_base_port,
        iec104_base_port=args.iec104_base_port,
        ads_host_prefix=args.ads_host_prefix,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
