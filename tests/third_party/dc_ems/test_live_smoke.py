"""可选择执行的 DCEMS Web/Modbus 真实进程烟雾测试。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from playwright.sync_api import Page, sync_playwright
from pymodbus.client import AsyncModbusTcpClient

from tests.third_party.dc_ems.websocket_observer import observe


@pytest.fixture(scope="module", autouse=True)
def live_only() -> None:
    if os.environ.get("DCEMS_LIVE") != "1":
        pytest.skip("set DCEMS_LIVE=1 after starting the isolated DCEMS stack")


def test_web_container_serves_application() -> None:
    """Nginx 静态资源必须真实可达；不等同于 Admin 功能已验收。"""
    url = os.environ.get("DCEMS_WEB_URL", "http://127.0.0.1:18080/dc-ems/")
    with urlopen(url, timeout=8) as response:
        document = response.read(512_000).decode("utf-8")
        assert response.status == 200
    assert "<html" in document.lower()
    assert "<script" in document.lower()


def test_web_api_does_not_return_gateway_error() -> None:
    """API 代理不得返回 502/504；登录鉴权及业务功能另行测试。"""
    base = os.environ.get("DCEMS_WEB_URL", "http://127.0.0.1:18080/dc-ems/")
    url = base.split("/dc-ems/")[0] + "/api/"
    try:
        with urlopen(url, timeout=8) as response:
            assert response.status not in {502, 503, 504}
    except HTTPError as exc:
        assert exc.code not in {502, 503, 504}


@pytest.mark.parametrize("device_index", range(10))
@pytest.mark.asyncio
async def test_gateway_modbus_random_input_changes(device_index: int) -> None:
    """真实 Gateway 端口应暴露动态随机数据，不用内存对象作真值。"""
    host = os.environ.get("DCEMS_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_GATEWAY_PORT", "15221")) + device_index
    # 专用烟雾配置：业务点 wind_speed = 1.3.0，F32 ABCD。
    address = 0
    client = AsyncModbusTcpClient(host, port=port, timeout=3)
    connected = await client.connect()
    assert connected, "Gateway Modbus TCP endpoint unavailable"
    try:
        observations: set[tuple[int, ...]] = set()
        for _ in range(4):
            result = await client.read_input_registers(address, count=2, device_id=1)
            assert not result.isError(), f"Modbus exception: {result}"
            observations.add(tuple(result.registers))
            await asyncio.sleep(0.6)
        assert (
            len(observations) >= 2
        ), f"wind speed frozen at Gateway {device_index + 1}: {observations}"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_station_power_and_primary_frequency_mode_over_gateway() -> None:
    """首台 Gateway 输出站控 P/Q 与调频模式；算法是否消费由独立测试验证。"""
    host = os.environ.get("DCEMS_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_GATEWAY_PORT", "15221"))
    client = AsyncModbusTcpClient(host, port=port, timeout=3)
    assert await client.connect()
    try:
        power_samples: set[tuple[int, ...]] = set()
        for _ in range(4):
            p = await client.read_input_registers(2, count=2, device_id=1)
            q = await client.read_input_registers(4, count=2, device_id=1)
            mode = await client.read_discrete_inputs(0, count=1, device_id=1)
            assert not p.isError()
            assert not q.isError()
            assert not mode.isError()
            assert mode.bits[0] in (True, False)
            power_samples.add(tuple(p.registers))
            await asyncio.sleep(0.6)
        assert len(power_samples) >= 2, "station active power did not change"
    finally:
        client.close()


def test_dcems_websocket_upgrades_through_local_proxy() -> None:
    """验证真实 /api/ws 反向代理完成 101 Upgrade，非仅页面静态资源可达。"""
    host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    handshake = (
        "GET /api/ws HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "\r\n"
    )
    with socket.create_connection((host, port), timeout=8) as client:
        client.settimeout(8)
        client.sendall(handshake.encode("ascii"))
        result = client.recv(4096)
    assert b"101 Switching Protocols" in result, result[:256]


def test_dcems_websocket_contains_live_ten_device_values() -> None:
    """真 Server 内存键必须连续变化；排除仅 Gateway 响应而 DCEMS 未采集。"""
    host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    observed = observe(host, port, seconds=16.0)
    for index in range(1, 11):
        key = f"WTG{index:02d}_emsWinSpd"
        assert len(observed.get(key, set())) >= 2, f"DCEMS not refreshing {key}"
    for key in (
        "SUBSTATION_powAct",
        "SUBSTATION_powReact",
        "PFR_isFreMode",
    ):
        assert observed.get(key), f"DCEMS signal never sampled: {key}"


def _float32(registers: list[int]) -> float:
    """按测试点表的 F32 ABCD 字序解码两个真实 Modbus 寄存器。"""
    return float(struct.unpack(">f", struct.pack(">HH", *registers))[0])


def _has_rounded_match(samples: list[float], observed: set[str], scale: float) -> bool:
    targets = {float(value) for value in observed}
    return any(
        abs(round(sample * scale, 2) - target) <= 0.001 for sample in samples for target in targets
    )


@pytest.mark.asyncio
async def test_gateway_and_dcems_numeric_reconciliation() -> None:
    """并发采样真实协议与 Server 推送，逐机防串台并核对站控工程单位。"""
    gateway_host = os.environ.get("DCEMS_GATEWAY_HOST", "127.0.0.1")
    gateway_port = int(os.environ.get("DCEMS_GATEWAY_PORT", "15221"))
    web_host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    web_port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    observer = asyncio.create_task(asyncio.to_thread(observe, web_host, web_port, seconds=12.0))
    clients = [
        AsyncModbusTcpClient(gateway_host, port=gateway_port + index, timeout=3)
        for index in range(10)
    ]
    wind_samples: dict[str, list[float]] = {f"WTG{index:02d}": [] for index in range(1, 11)}
    station_samples: dict[str, list[float]] = {"powAct": [], "powReact": []}
    gateway_modes: set[bool] = set()
    try:
        connected = await asyncio.gather(*(client.connect() for client in clients))
        assert all(connected), "one or more Gateway Modbus endpoints unavailable"
        await asyncio.sleep(0.2)
        for _ in range(18):
            results = await asyncio.gather(
                *(client.read_input_registers(0, count=2, device_id=1) for client in clients)
            )
            for index, result in enumerate(results, 1):
                assert not result.isError(), f"WTG{index:02d} wind read failed: {result}"
                wind_samples[f"WTG{index:02d}"].append(_float32(result.registers))
            p, q, mode = await asyncio.gather(
                clients[0].read_input_registers(2, count=2, device_id=1),
                clients[0].read_input_registers(4, count=2, device_id=1),
                clients[0].read_discrete_inputs(0, count=1, device_id=1),
            )
            assert not p.isError() and not q.isError() and not mode.isError()
            station_samples["powAct"].append(_float32(p.registers))
            station_samples["powReact"].append(_float32(q.registers))
            gateway_modes.add(mode.bits[0])
            await asyncio.sleep(0.4)
    finally:
        for client in clients:
            client.close()
    observed = await observer

    matches: dict[str, bool] = {}
    for device_id, samples in wind_samples.items():
        values = observed.get(f"{device_id}_emsWinSpd", set())
        matches[device_id] = _has_rounded_match(samples, values, 1.0)
        assert matches[device_id], f"Gateway/DCEMS mismatch or cross-wiring: {device_id}"
        assert observed.get(f"{device_id}_emsWorkMode") == {
            "32"
        }, f"unexpected DCEMS work mode: {device_id}"
    for key, scale in (("powAct", 0.001), ("powReact", 0.001)):
        matches[key] = _has_rounded_match(
            station_samples[key], observed.get(f"SUBSTATION_{key}", set()), scale
        )
        assert matches[key], f"station unit conversion mismatch: {key}"
    assert gateway_modes == {False}, f"unsafe or unexpected PFR mode input: {gateway_modes}"
    assert observed.get("PFR_isFreMode") == {"false"}

    evidence = {
        "wind": {
            device_id: {
                "gateway_mps": samples,
                "dcems_mps": sorted(observed.get(f"{device_id}_emsWinSpd", set())),
                "matched": matches[device_id],
            }
            for device_id, samples in wind_samples.items()
        },
        "station": {
            key: {
                "gateway_w_or_var": station_samples[key],
                "dcems_kw_or_kvar": sorted(observed.get(f"SUBSTATION_{key}", set())),
                "scale": 0.001,
                "matched": matches[key],
            }
            for key in station_samples
        },
        "gateway_pfr_modes": sorted(gateway_modes),
        "dcems_pfr_modes": sorted(observed.get("PFR_isFreMode", set())),
    }
    evidence_dir = Path(os.environ.get("DCEMS_EVIDENCE_DIR", "run/dcems-smoke/evidence"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "numeric-reconciliation.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _login(page: Page) -> None:
    """使用调用方注入的真实账户登录，不在源码或证据中保存口令。"""
    password = os.environ.get("DCEMS_WEB_PASSWORD")
    assert password, "set DCEMS_WEB_PASSWORD from the isolated runtime seed"
    page.locator("input[name=username]").fill(os.environ.get("DCEMS_WEB_USERNAME", "admin"))
    page.locator("input[name=password]").fill(password)
    page.get_by_role("button", name="登录").click()
    page.wait_for_url("**/dashboard")


def test_browser_ten_turbines_are_numeric_and_refresh() -> None:
    """真实浏览器逐行验收十机数值与刷新，不能用 WebSocket 101 代替。"""
    host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    evidence_dir = (
        Path(os.environ.get("DCEMS_EVIDENCE_DIR", "run/dcems-smoke/evidence")) / "browser"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(f"http://{host}:{port}/dc-ems/#/login", wait_until="networkidle")
            _login(page)
            page.goto(f"http://{host}:{port}/dc-ems/#/AGC/index")
            page.get_by_text("单机视图", exact=True).click()
            page.wait_for_timeout(5000)
            device_ids = [f"WTG{index:02d}" for index in range(1, 11)]
            samples: dict[str, list[float]] = {device_id: [] for device_id in device_ids}
            rows: dict[str, str] = {}
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                for device_id in device_ids:
                    row = page.locator(".el-table__body-wrapper tr").filter(
                        has=page.get_by_text(device_id, exact=True)
                    )
                    if row.count() != 1:
                        continue
                    rows[device_id] = row.inner_text(timeout=1000)
                    match = re.search(r"(-?\d+(?:\.\d+)?)\s*m/s", rows[device_id])
                    if match:
                        samples[device_id].append(float(match.group(1)))
                if all(len(set(values)) >= 2 for values in samples.values()):
                    break
                page.wait_for_timeout(500)
            page.screenshot(path=str(evidence_dir / "browser-ten-turbines.png"), full_page=True)
            (evidence_dir / "browser-ten-turbines.json").write_text(
                json.dumps(
                    {"last_rows": rows, "wind_speed_samples": samples},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            missing = [device_id for device_id, values in samples.items() if not values]
            frozen = [
                device_id for device_id, values in samples.items() if len(set(values)) == 1
            ]
            assert not missing and not frozen, f"missing={missing}, frozen={frozen}"
        finally:
            browser.close()


def test_browser_station_power_is_visible_and_refreshes() -> None:
    """首页必须显示真实升压站 P/Q 的工程单位及动态刷新。"""
    host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    evidence_dir = (
        Path(os.environ.get("DCEMS_EVIDENCE_DIR", "run/dcems-smoke/evidence")) / "browser"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(f"http://{host}:{port}/dc-ems/#/login", wait_until="networkidle")
            _login(page)
            page.wait_for_timeout(4000)
            first = page.locator("body").inner_text()
            page.wait_for_timeout(2500)
            second = page.locator("body").inner_text()
            page.screenshot(path=str(evidence_dir / "browser-station-power.png"), full_page=True)
            (evidence_dir / "browser-station-power.json").write_text(
                json.dumps({"first": first, "second": second}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for label, unit in (("升压站有功", "kW"), ("升压站无功", "kVar")):
                pattern = rf"{label}\s+(-?\d+(?:\.\d+)?)\s*{unit}"
                initial = re.search(pattern, first)
                refreshed = re.search(pattern, second)
                assert initial and refreshed, f"browser station value missing: {label}"
                assert refreshed.group(1) != initial.group(
                    1
                ), f"browser station value frozen: {label}"
        finally:
            browser.close()


def test_factory_primary_frequency_mode_matches_false_input() -> None:
    """独立区分设备布尔值与全场变量，不允许缺失键冒充 false。"""
    host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    keys = ["PFR_isFreMode", "fp_emsIsFreMod"]
    observed = observe(host, port, seconds=12.0, keys=keys)
    evidence_dir = Path(os.environ.get("DCEMS_EVIDENCE_DIR", "run/dcems-smoke/evidence"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "pfr-factory-values.json").write_text(
        json.dumps(
            {key: sorted(observed.get(key, set())) for key in keys},
            indent=2,
        ),
        encoding="utf-8",
    )
    for key in keys:
        assert observed.get(key) == {"false"}, f"unexpected or missing PFR value: {key}"


def test_browser_primary_frequency_mode_matches_false_input() -> None:
    """调频页面必须显示 FC02 false，而不是空值或把内部使能当作外部模式。"""
    host = os.environ.get("DCEMS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_WEB_PORT", "18080"))
    evidence_dir = (
        Path(os.environ.get("DCEMS_EVIDENCE_DIR", "run/dcems-smoke/evidence")) / "browser"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(f"http://{host}:{port}/dc-ems/#/login", wait_until="networkidle")
            _login(page)
            page.goto(f"http://{host}:{port}/dc-ems/#/primaryFrequencyModulation/index")
            page.wait_for_timeout(5000)
            text = page.locator("body").inner_text()
            page.screenshot(path=str(evidence_dir / "browser-pfr-mode.png"), full_page=True)
            (evidence_dir / "browser-pfr-mode.txt").write_text(text, encoding="utf-8")
            assert re.search(
                r"调频信号\s+false", text
            ), "browser does not display the externally sampled false PFR mode"
        finally:
            browser.close()
