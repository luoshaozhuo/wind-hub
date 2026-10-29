"""配置驱动的只读自动通信诊断，失败即停并保留可复查证据。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.application.config_snapshot import ConfigSnapshot

from .inventory import devices, points, sinks
from .network_diagnostic import tcp
from .protocol_diagnostic import execute


def plan(snapshot: ConfigSnapshot) -> list[dict[str, str]]:
    """稳定步骤标识：设备连接、点位读取、Sink 连接前置检查。"""
    steps: list[dict[str, str]] = []
    for device in devices(snapshot):
        device_id = str(device["device_id"])
        if not device["enabled"]:
            continue
        steps.append({"kind": "protocol_check", "target": device_id, "point": ""})
        for item in points(snapshot, device_id):
            if item["access"] in {"read", "read_write"}:
                steps.append({
                    "kind": "point_read",
                    "target": device_id,
                    "point": str(item["point_id"]),
                })
    for sink in sinks(snapshot):
        if not sink["enabled"]:
            continue
        # Sink 本身为本地监听端口时，不能用出站 TCP connect 证明链路正常。
        if sink["kind"] not in {"redis", "tdengine", "kafka", "grpc"}:
            continue
        steps.append({
            "kind": "sink_endpoint",
            "target": str(sink["sink_id"]),
            "point": "",
        })
    return steps


def _sink_address(info: dict[str, Any]) -> tuple[str, int] | None:
    connection = info["connection"]
    host = connection.get("host")
    port = connection.get("port")
    if isinstance(host, str) and isinstance(port, int):
        return host, port
    endpoint = connection.get("endpoint", connection.get("bootstrap_servers"))
    if isinstance(endpoint, str):
        first = endpoint.split(",")[0].strip()
        if first.count(":") == 1:
            target, port_text = first.rsplit(":", 1)
            if port_text.isdecimal():
                return target, int(port_text)
    return None


async def run(
    snapshot: ConfigSnapshot,
    fingerprint: str,
    *,
    previous: dict[str, Any] | None = None,
    restart: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """相同指纹只重试失败步骤；指纹变化或 restart 则从头执行。"""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    planned = plan(snapshot)
    start = 0
    old_steps: list[dict[str, Any]] = []
    if previous is not None and not restart and previous.get("fingerprint") == fingerprint:
        failed = previous.get("failed_index")
        if isinstance(failed, int) and 0 <= failed < len(planned):
            old_steps = previous.get("steps", [])[:failed]
            expected = [
                (entry["kind"], entry["target"], entry["point"])
                for entry in planned[:failed]
            ]
            actual = [
                (entry["kind"], entry["target"], entry["point"])
                for entry in old_steps
            ]
            if expected == actual:
                start = failed
            else:
                old_steps = []

    steps = list(old_steps)
    sink_by_id = {str(item["sink_id"]): item for item in sinks(snapshot)}
    device_by_id = {str(item["device_id"]): item for item in devices(snapshot)}
    for index in range(start, len(planned)):
        step = planned[index]
        target = step["target"]
        kind = step["kind"]
        entry: dict[str, Any] = {
            **step, "index": index, "status": "PASS",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            if kind in {"protocol_check", "point_read"}:
                device = device_by_id[target]
                entry["endpoint"] = {
                    "host": device["host"], "port": device["port"],
                }
                entry["protocol"] = device["protocol"]
                entry["protocol_options"] = device["protocol_options"]
                operation = "check" if kind == "protocol_check" else "read"
                data = await execute(
                    snapshot, target, operation,
                    point_ids=(step["point"],) if kind == "point_read" else (),
                    timeout=timeout,
                )
                entry["data"] = data
                if any(
                    sample.get("quality", "good").lower() != "good"
                    for sample in data if isinstance(sample, dict)
                ):
                    entry["status"] = "WARNING"
            else:
                sink = sink_by_id[target]
                entry["sink_kind"] = sink["kind"]
                address = _sink_address(sink)
                if address is None:
                    entry["status"] = "UNKNOWN"
                    entry["message"] = "Sink endpoint not representable as a single TCP address"
                else:
                    entry["endpoint"] = {"host": address[0], "port": address[1]}
                    entry["data"] = await tcp(*address, timeout=timeout)
        except Exception as exc:
            entry["status"] = "FAIL"
            entry["code"] = type(exc).__name__
            entry["message"] = str(exc)
        steps.append(entry)
        if entry["status"] == "FAIL":
            return {
                "status": "FAIL",
                "fingerprint": fingerprint,
                "failed_index": index,
                "total": len(planned),
                "completed": len(steps),
                "steps": steps,
            }
    has_unknown = any(item["status"] == "UNKNOWN" for item in steps)
    has_warning = any(item["status"] == "WARNING" for item in steps)
    return {
        "status": "UNKNOWN" if has_unknown else "WARNING" if has_warning else "PASS",
        "fingerprint": fingerprint,
        "failed_index": None,
        "total": len(planned),
        "completed": len(steps),
        "scope": "configured device protocol IO and selected sink TCP endpoints",
        "limitations": (
            "TCP connectivity is not evidence of end-to-end data delivery; "
            "service runtime and business data correctness are not checked."
        ),
        "steps": steps,
    }
