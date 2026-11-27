"""生成只供真实 DCEMS Server 控制出口联测的单风机隔离种子。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, cast

from tests.third_party.dc_ems.prepare_wtg_control_chain import CONTROL_PORT

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "third_party/dc-ems/app/server/data.json"
DCEMS_DEVICE_ID = "WTG01"
PROTOCOL_POINTS = frozenset({"ctrlActPow", "ctrlReActPow"})
CONTROL_FLAGS = frozenset({"emsActPowerEnable", "emsReactPowerEnable", "bFreCtrlEnableFlag"})


def build_seed(original: dict[str, Any], host: str) -> dict[str, Any]:
    """不改原始设备代码，只派生最小协议写入验收用数据。"""
    if host not in {"127.0.0.1", "host.docker.internal"}:
        raise ValueError("DCEMS control fixture only supports isolated loopback host")
    seed = json.loads(json.dumps(original))
    if not seed.get("devices") or not seed.get("edgeServers"):
        raise ValueError("DCEMS seed has no devices or channels")

    controls = [point for point in seed["edgeaddrset"] if point.get("isCtrl") is True]
    if not {"ctrlActPow", "ctrlReActPow"} <= {point["varPath"] for point in controls}:
        raise ValueError("published DCEMS control points unavailable")
    original_points = seed["edgeaddrset"]
    selected = [item for item in original_points if item["varPath"] in PROTOCOL_POINTS]
    if {item["varPath"] for item in selected} != PROTOCOL_POINTS:
        raise ValueError("required DCEMS Modbus read/write points missing")
    for item in selected:
        if item.get("isCtrl") is not True or item.get("highBit") != 1:
            raise ValueError("control must use original CDAB writable point")

    device = seed["devices"][0]
    device["deviceId"] = DCEMS_DEVICE_ID
    device["deviceName"] = "isolated-DCEMS-WTG01"
    device["serverId"] = 1
    device["addrSet"] = selected[0]["setId"]
    device["lineNo"] = 1
    device["capacity"] = 2000.0
    seed["devices"] = [device]

    endpoint = seed["edgeServers"][0]
    endpoint.update(
        serverName="dcems-wtg-control-isolated",
        serverIp1=host,
        port1=CONTROL_PORT,
        serverIp2=None,
        port2=None,
        serverType="MODBUSTCP",
        interval=500,
    )
    seed["edgeServers"] = [endpoint]
    seed["edgeaddrset"] = selected
    seed["syncdata"] = []
    seed["sysconfig"] = [
        item
        for item in seed["sysconfig"]
        if not (
            item.get("groupName") in {"sync", "watch"}
            and "redis" in str(item.get("itemName", "")).lower()
        )
    ]

    for item in seed["emsConfig"]:
        if item["varPath"] == "iWTUnits" or item["varPath"] == "iLineCount":
            item["val"] = "1"
        elif item["varPath"] in CONTROL_FLAGS or item["varPath"] == "kk_flag":
            item["val"] = "false"
        elif item["varPath"] == "license":
            item["val"] = os.environ.get("DCEMS_LICENSE_CODE", "")
    flags = [item for item in seed["emsConfig"] if item["varPath"] in CONTROL_FLAGS]
    if len(flags) != 3 or {item["varPath"] for item in flags} != CONTROL_FLAGS:
        raise ValueError("missing mandatory disabled automatic-control flags")
    return cast(dict[str, Any], seed)


def main() -> None:
    """只允许写入新文件；不得覆盖 dc-ems 已发布种子。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gateway-host", choices=["127.0.0.1", "host.docker.internal"], required=True
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if (ROOT / "run").resolve() not in output.parents:
        raise ValueError("DCEMS derived seed must be written under wind-hub run/")
    if output == SOURCE.resolve():
        raise ValueError("refuse to overwrite published DCEMS seed")
    if output.exists():
        raise FileExistsError(output)
    document = json.loads(SOURCE.read_text(encoding="utf-8"))
    seed = build_seed(document, args.gateway_host)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        json.dump(seed, target, indent=2, ensure_ascii=False)
    output.chmod(0o600)
    print(output)


if __name__ == "__main__":
    main()
