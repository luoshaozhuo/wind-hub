"""串行、只读的配置驱动通信诊断；首个失败项停止并保留证据。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.protocol_contract import ProtocolCapability

from .inventory import devices, points, sinks
from .network_diagnostic import tcp
from .protocol_diagnostic import create_driver, prepare_ads, release_ads, sample_dict


def plan(snapshot: ConfigSnapshot) -> list[dict[str, str]]:
    """确定性步骤列表；配置快照变化必须重建计划。"""
    steps: list[dict[str, str]] = []
    for device in devices(snapshot):
        device_id = str(device["device_id"])
        if not device["enabled"]:
            continue
        steps.append({"kind": "protocol_check", "target": device_id, "point": ""})
        for item in points(snapshot, device_id):
            if item["access"] in {"read", "read_write"}:
                steps.append(
                    {
                        "kind": "point_read",
                        "target": device_id,
                        "point": str(item["point_id"]),
                    }
                )
    for sink in sinks(snapshot):
        if not sink["enabled"]:
            continue
        steps.append(
            {
                "kind": (
                    "sink_endpoint"
                    if sink["kind"] in {"redis", "tdengine", "kafka", "grpc"}
                    else "sink_not_tested"
                ),
                "target": str(sink["sink_id"]),
                "point": "",
            }
        )
    return steps


def _sink_address(sink: dict[str, Any]) -> tuple[str, int] | None:
    connection = sink["connection"]
    host, port = connection.get("host"), connection.get("port")
    if isinstance(host, str) and isinstance(port, int):
        return host, port
    address = connection.get("endpoint", connection.get("bootstrap_servers"))
    if isinstance(address, str):
        first = address.split(",")[0].strip()
        if first.count(":") == 1:
            host, raw_port = first.rsplit(":", 1)
            if raw_port.isdecimal():
                return host, int(raw_port)
    return None


def _resume(
    planned: list[dict[str, str]],
    fingerprint: str,
    previous: dict[str, Any] | None,
    restart: bool,
) -> tuple[int, list[dict[str, Any]]]:
    if previous is None or restart or previous.get("fingerprint") != fingerprint:
        return 0, []
    failed = previous.get("failed_index")
    if not isinstance(failed, int) or not 0 <= failed < len(planned):
        return 0, []
    saved = previous.get("steps", [])
    if not isinstance(saved, list):
        return 0, []
    saved = saved[:failed]
    actual = [(step.get("kind"), step.get("target"), step.get("point")) for step in saved]
    expected = [(step["kind"], step["target"], step["point"]) for step in planned[:failed]]
    if actual != expected:
        return 0, []
    return failed, list(saved)


async def run(
    snapshot: ConfigSnapshot,
    fingerprint: str,
    *,
    previous: dict[str, Any] | None = None,
    restart: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """每台设备单独建立并复用一次连接，点位顺序稳定。"""
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be within 0..300 seconds")
    planned = plan(snapshot)
    start, steps = _resume(planned, fingerprint, previous, restart)
    devices_by_id = {str(item["device_id"]): item for item in devices(snapshot)}
    sinks_by_id = {str(item["sink_id"]): item for item in sinks(snapshot)}
    current_driver: Any = None
    current_target: str | None = None
    ads_prepared = False

    async def close_driver() -> None:
        nonlocal current_driver, current_target, ads_prepared
        driver = current_driver
        current_driver = None
        current_target = None
        try:
            if driver is not None:
                async with asyncio.timeout(timeout):
                    await driver.close()
        finally:
            try:
                await release_ads(ads_prepared)
            finally:
                ads_prepared = False

    try:
        for index in range(start, len(planned)):
            step = planned[index]
            target, kind = step["target"], step["kind"]
            entry: dict[str, Any] = {
                **step,
                "index": index,
                "status": "PASS",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                if kind in {"protocol_check", "point_read"}:
                    device = devices_by_id[target]
                    entry["protocol"] = device["protocol"]
                    entry["endpoint"] = {
                        "host": device["host"],
                        "port": device["port"],
                    }
                    entry["protocol_options"] = device["protocol_options"]
                    if current_target != target:
                        await close_driver()
                        current_driver = create_driver(snapshot, target)
                        ads_prepared = await prepare_ads(snapshot, current_driver)
                        async with asyncio.timeout(timeout):
                            await current_driver.connect()
                        current_target = target
                    if kind == "protocol_check":
                        async with asyncio.timeout(timeout):
                            health = await current_driver.health()
                        if not health.healthy:
                            raise RuntimeError(health.message or "protocol health failed")
                        entry["data"] = {"healthy": True, "message": health.message}
                    else:
                        if ProtocolCapability.READ not in current_driver.capabilities():
                            raise RuntimeError("protocol does not support active read")
                        async with asyncio.timeout(timeout):
                            sample = await current_driver.read_one(step["point"])
                        entry["data"] = sample_dict(sample)
                        if sample.quality.value != "good":
                            entry["status"] = "WARNING"
                else:
                    await close_driver()
                    sink = sinks_by_id[target]
                    entry["sink_kind"] = sink["kind"]
                    address = _sink_address(sink) if kind == "sink_endpoint" else None
                    if kind == "sink_not_tested":
                        entry["status"] = "UNKNOWN"
                        entry["message"] = (
                            f"Sink type {sink['kind']} is not covered by outbound TCP probes"
                        )
                    elif address is None:
                        entry["status"] = "UNKNOWN"
                        entry["message"] = "No unambiguous TCP endpoint in Sink configuration"
                    else:
                        entry["endpoint"] = {"host": address[0], "port": address[1]}
                        entry["data"] = await tcp(*address, timeout=timeout)
            except Exception as exc:
                entry["status"] = "FAIL"
                entry["code"] = type(exc).__name__
                entry["message"] = str(exc)
            steps.append(entry)
            if entry["status"] == "FAIL":
                return _result(fingerprint, planned, steps, index)
    finally:
        await close_driver()
    return _result(fingerprint, planned, steps, None)


def _result(
    fingerprint: str,
    planned: list[dict[str, str]],
    steps: list[dict[str, Any]],
    failed_index: int | None,
) -> dict[str, Any]:
    status = (
        "FAIL"
        if failed_index is not None
        else "UNKNOWN"
        if any(step["status"] == "UNKNOWN" for step in steps)
        else "WARNING"
        if any(step["status"] == "WARNING" for step in steps)
        else "PASS"
    )
    return {
        "status": status,
        "fingerprint": fingerprint,
        "failed_index": failed_index,
        "total": len(planned),
        "completed": len(steps),
        "scope": "device protocol checks, configured reads, and outbound Sink TCP endpoints",
        "limitations": (
            "TCP availability does not verify data delivery or service runtime; "
            "control writes and business data checks are excluded."
        ),
        "steps": steps,
    }
