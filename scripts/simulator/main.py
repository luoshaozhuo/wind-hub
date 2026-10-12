#!/usr/bin/env python
"""wind-hub 多协议 PLC 模拟器入口。

用法（仓库根目录）::

    python3 scripts/simulator/main.py start      # 前台运行全部 40 台设备
    python3 scripts/simulator/main.py status     # 运行状态（state.json）
    python3 scripts/simulator/main.py devices    # 设备清单
    python3 scripts/simulator/main.py fault sim-mb-03 down      # 注入故障
    python3 scripts/simulator/main.py fault sim-mb-03 freeze
    python3 scripts/simulator/main.py recover sim-mb-03         # 清除故障
    python3 scripts/simulator/main.py stop       # 停止运行中的模拟器

配套配置树 ``configs/simulation/`` 由 ``scripts/simulator/gen_config.py``
生成（拓扑变化时重跑）。

运行状态与故障命令文件在 ``run/simulator/``（pid / state.json / faults.json）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.simulator.devices import (  # noqa: E402
    ADS_TCP_PORT,
    AdsSimDevice,
    IEC104SimDevice,
    ModbusSimDevice,
    SimDevice,
)

RUN_DIR = REPO_ROOT / "run" / "simulator"
FAULTS_PATH = RUN_DIR / "faults.json"
STATE_PATH = RUN_DIR / "state.json"
PID_PATH = RUN_DIR / "simulator.pid"

MODBUS_COUNT = 20
ADS_COUNT = 10
IEC104_COUNT = 10
MODBUS_BASE_PORT = 10502
IEC104_BASE_PORT = 12404
ADS_BASE_HOST = 10  # 127.0.0.11 起


def build_devices(
    modbus: int = MODBUS_COUNT, ads: int = ADS_COUNT, iec104: int = IEC104_COUNT
) -> list[SimDevice]:
    """默认拓扑：20 Modbus + 10 ADS + 10 IEC104（seed 由序号决定，可复现）。"""
    devices: list[SimDevice] = []
    for i in range(modbus):
        devices.append(
            ModbusSimDevice(
                device_id=f"sim-mb-{i + 1:02d}",
                protocol="modbus",
                host="127.0.0.1",
                port=MODBUS_BASE_PORT + i,
                seed=1000 + i,
            )
        )
    for i in range(iec104):
        devices.append(
            IEC104SimDevice(
                device_id=f"sim-i104-{i + 1:02d}",
                protocol="iec104",
                host="127.0.0.1",
                port=IEC104_BASE_PORT + i,
                seed=2000 + i,
            )
        )
    for i in range(ads):
        devices.append(
            AdsSimDevice(
                device_id=f"sim-ads-{i + 1:02d}",
                protocol="ads",
                host=f"127.0.0.{ADS_BASE_HOST + 1 + i}",
                port=ADS_TCP_PORT,
                seed=3000 + i,
            )
        )
    return devices


def _read_faults() -> dict[str, str]:
    if not FAULTS_PATH.is_file():
        return {}
    try:
        return dict(json.loads(FAULTS_PATH.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_faults(faults: dict[str, str]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    FAULTS_PATH.write_text(json.dumps(faults, indent=2), encoding="utf-8")


def _device_index() -> dict[str, SimDevice]:
    return {d.device_id: d for d in build_devices()}


async def _run_foreground(tick_s: float) -> int:
    devices = build_devices()
    by_id = {d.device_id: d for d in devices}
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    started: set[str] = set()
    for device in devices:
        try:
            await device.start_server()
            started.add(device.device_id)
        except Exception:
            print(f"启动失败: {device.device_id}", file=sys.stderr)
            raise
    print(f"模拟器已启动：{len(started)} 台设备（tick {tick_s}s）", flush=True)

    stop = asyncio.Event()

    def _sigterm() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _sigterm)

    applied_faults: dict[str, str] = {}
    try:
        while not stop.is_set():
            # 应用故障文件（fault/recover 子命令的唯一通道）。
            desired = _read_faults()
            for device_id, kind in desired.items():
                if device_id not in by_id:
                    continue
                if applied_faults.get(device_id) != kind:
                    device = by_id[device_id]
                    if kind == "down":
                        await device.stop_server()
                    elif kind == "freeze":
                        device.freeze()
                    device.fault = kind
                    applied_faults[device_id] = kind
                    print(f"fault 注入: {device_id} -> {kind}", flush=True)
            for device_id in set(applied_faults) - set(desired):
                device = by_id[device_id]
                device.unfreeze()
                if applied_faults[device_id] == "down":
                    await device.start_server()
                device.fault = None
                del applied_faults[device_id]
                print(f"fault 恢复: {device_id}", flush=True)

            # 数据推进（各设备独立故障状态）。
            for device in devices:
                try:
                    await device.tick(tick_s)
                except Exception as exc:  # 单设备刷新失败不影响整体
                    print(f"tick 失败 {device.device_id}: {exc}", file=sys.stderr)

            STATE_PATH.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "device_count": len(devices),
                        "devices": [
                            {
                                "device_id": d.device_id,
                                "protocol": d.protocol,
                                "endpoint": f"{d.host}:{d.port}",
                                "fault": d.fault,
                            }
                            for d in devices
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=tick_s)
    finally:
        for device in devices:
            if device.device_id in started:
                await device.stop_server()
        for path in (PID_PATH, STATE_PATH):
            path.unlink(missing_ok=True)
    print("模拟器已停止", flush=True)
    return 0


def _cmd_status() -> int:
    if not STATE_PATH.is_file():
        print("模拟器未运行（无 state.json）")
        return 1
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    faults = {d["device_id"]: d["fault"] for d in state["devices"] if d["fault"]}
    print(
        f"运行中 pid={state['pid']} devices={state['device_count']} "
        f"updated={state['updated_at']}"
    )
    if faults:
        print("故障注入中:")
        for device_id, kind in faults.items():
            print(f"  {device_id}: {kind}")
    else:
        print("无故障注入")
    return 0


def _cmd_devices() -> int:
    for device in build_devices():
        print(f"{device.device_id}\t{device.protocol}\t{device.host}:{device.port}")
    return 0


def _cmd_fault(device_id: str, kind: str) -> int:
    index = _device_index()
    if device_id not in index:
        print(f"未知设备 {device_id}（见 devices 子命令）", file=sys.stderr)
        return 2
    faults = _read_faults()
    faults[device_id] = kind
    _write_faults(faults)
    print(f"已请求故障 {device_id} -> {kind}（运行进程下一 tick 生效）")
    return 0


def _cmd_recover(device_id: str) -> int:
    faults = _read_faults()
    if faults.pop(device_id, None) is None:
        print(f"{device_id} 无故障记录")
        return 1
    _write_faults(faults)
    print(f"已请求恢复 {device_id}")
    return 0


def _cmd_stop() -> int:
    if not PID_PATH.is_file():
        print("模拟器未运行（无 pid 文件）")
        return 1
    pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    os.kill(pid, signal.SIGTERM)
    print(f"已向 pid={pid} 发送 SIGTERM")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start", help="前台启动全部模拟设备")
    start_p.add_argument("--tick", type=float, default=1.0, help="数据推进周期（秒）")
    sub.add_parser("status", help="运行状态")
    sub.add_parser("devices", help="设备清单")
    fault_p = sub.add_parser("fault", help="注入故障")
    fault_p.add_argument("device_id")
    fault_p.add_argument("kind", choices=["down", "freeze"])
    recover_p = sub.add_parser("recover", help="清除故障")
    recover_p.add_argument("device_id")
    sub.add_parser("stop", help="停止运行中的模拟器")
    args = parser.parse_args()

    if args.command == "start":
        return asyncio.run(_run_foreground(args.tick))
    if args.command == "status":
        return _cmd_status()
    if args.command == "devices":
        return _cmd_devices()
    if args.command == "fault":
        return _cmd_fault(args.device_id, args.kind)
    if args.command == "recover":
        return _cmd_recover(args.device_id)
    return _cmd_stop()


if __name__ == "__main__":
    sys.exit(main())
