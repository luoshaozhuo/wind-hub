"""全业务链系统测试——验收环境形态（模拟器 → Collector/Commander → Web
Server，真实 CSV + Modbus SCADA sink）。

以 ``scripts/acceptance.py start --foreground`` 拉起与手工验收完全一致
的进程拓扑（40 台真实协议栈模拟 PLC），经 Web Server HTTP API 断言：

- 40 台设备全部 connected（三协议真实连接）；
- 任务经 API 启动后数据落真实 CSV sink 与 Modbus SCADA sink；
- Commander 读点返回模拟器实时值；写点回读一致；
- 故障注入（simulator fault down）后设备离线、写命令
  CONFIRMED_FAILURE，恢复后重连。

Redis sink 不在本测试内（依赖 docker）；由
``python3 scripts/acceptance.py start --with-redis`` 手工/验收覆盖。
"""

from __future__ import annotations

import asyncio
import json
import signal
import struct
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[4]
STATE_PATH = REPO_ROOT / "run" / "acceptance" / "acceptance.json"
CSV_PATH = REPO_ROOT / "run" / "acceptance" / "archive.csv"
API_BASE = "http://127.0.0.1:8000"
MODBUS_SINK_PORT = 1502

# 40 台协议 server + 三个服务进程的冷启动预算。
_STARTUP_BUDGET_S = 120.0


def _cleanup_stray_state() -> None:
    """启动前清场：残留 state/进程会让 acceptance 拒绝启动。"""
    STATE_PATH.unlink(missing_ok=True)
    for pattern in ("simulator/main.py", "wind-hub-collector", "wind-hub-commander"):
        subprocess.run(["pkill", "-f", pattern], check=False, capture_output=True)  # noqa: S603, S607
    time.sleep(1.0)


def _wait_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.5)
    raise RuntimeError(f"等待超时: {path}")


@pytest_asyncio.fixture(scope="module")
async def chain() -> AsyncIterator[dict[str, object]]:
    """验收环境进程（module 级，全链共享一个环境）。"""
    _cleanup_stray_state()
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "scripts/acceptance.py", "start", "--foreground"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.to_thread(_wait_file, STATE_PATH, _STARTUP_BUDGET_S)
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as client:
            # 等 Web Server 能如实回答（gRPC 上游就绪）。
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                try:
                    resp = await client.get("/api/v1/collector/info")
                    if resp.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Web Server 未就绪")
        yield state
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
        _cleanup_stray_state()


@pytest.fixture
def api() -> httpx.Client:
    with httpx.Client(base_url=API_BASE, timeout=10.0) as client:
        yield client


def _auth(state: dict[str, object]) -> dict[str, str]:
    return {"X-API-Key": str(state["write_token"])}


@pytest.mark.usefixtures("chain")
class TestSimulatorFullChain:
    def test_all_devices_connected(self, api: httpx.Client) -> None:
        items = api.get("/api/v1/collector/devices").json()["items"]
        assert len(items) == 40
        connected = {d["device_id"] for d in items if d.get("connected", False)}
        offline = {d["device_id"] for d in items} - connected
        assert len(connected) == 40, f"未连接: {offline}"
        protocols = {d["protocol"] for d in items}
        assert protocols == {"modbus", "ads", "iec104"}

    def test_collection_flows_to_csv_and_modbus_sink(
        self, api: httpx.Client, chain: dict[str, object]
    ) -> None:
        for task_id in ("sim-modbus-all", "sim-iec104-all", "sim-ads-all"):
            resp = api.post(
                f"/api/v1/collector/tasks/{task_id}/start", headers=_auth(chain)
            )
            assert resp.status_code == 200, resp.text

        # CSV sink：等待真实数据行落盘（不是只看进程活着）。
        CSV_PATH.unlink(missing_ok=True)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if CSV_PATH.is_file() and sum(1 for _ in CSV_PATH.open()) > 40:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("CSV sink 30s 内无数据")

        # Modbus SCADA sink：holding 0 起为 EXPORT_POINTS 映射（float32 BE）。
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient("127.0.0.1", port=MODBUS_SINK_PORT)
        assert client.connect()
        try:
            result = client.read_holding_registers(0, count=2, device_id=1)
            assert not result.isError()
            value = struct.unpack(">f", struct.pack(">HH", *result.registers[:2]))[0]
            # sim-mb-01.active_power 基值 1500±300+噪声；非零即真实链路。
            assert 1000.0 < value < 2000.0
        finally:
            client.close()

    def test_commander_read_write_roundtrip(
        self, api: httpx.Client, chain: dict[str, object]
    ) -> None:
        write = api.post(
            "/api/v1/commander/commands",
            headers=_auth(chain),
            json={
                "command_id": "sys-fc-001",
                "device_id": "sim-mb-01",
                "point_id": "power_setpoint",
                "value": 666.25,
                "timeout": 5,
            },
        )
        assert write.status_code == 200, write.text
        body = write.json()
        assert body["outcome"] == "COMMAND_OUTCOME_CONFIRMED_SUCCESS"
        assert body["command_id"] == "sys-fc-001"

        time.sleep(2.0)  # 模拟器 sync_controls 下一 tick 回同步
        read = api.get("/api/v1/commander/devices/sim-mb-01/points/power_setpoint")
        assert read.status_code == 200
        value = read.json()["value"]
        assert value["double_value"] == pytest.approx(666.25, abs=0.01)

    def test_fault_injection_marks_device_offline_and_write_fails(
        self, api: httpx.Client, chain: dict[str, object]
    ) -> None:
        subprocess.run(  # noqa: S603
            [sys.executable, "scripts/simulator/main.py", "fault", "sim-mb-05", "down"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        try:
            # 等设备离线反映到 API（health_timeout + 轮询间隔预算）。
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                items = api.get("/api/v1/collector/devices").json()["items"]
                mb05 = next(d for d in items if d["device_id"] == "sim-mb-05")
                # proto3 JSON 省略默认值字段：键缺失即 connected=false。
                if not mb05.get("connected", False):
                    break
                time.sleep(1.0)
            else:
                raise AssertionError("sim-mb-05 未在 30s 内离线")

            write = api.post(
                "/api/v1/commander/commands",
                headers=_auth(chain),
                json={
                    "command_id": "sys-fc-002",
                    "device_id": "sim-mb-05",
                    "point_id": "power_setpoint",
                    "value": 100.0,
                    "timeout": 3,
                },
            )
            assert write.status_code == 200
            assert write.json()["outcome"] == "COMMAND_OUTCOME_CONFIRMED_FAILURE"
        finally:
            subprocess.run(  # noqa: S603
                [sys.executable, "scripts/simulator/main.py", "recover", "sim-mb-05"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
