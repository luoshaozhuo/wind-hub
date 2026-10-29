"""CLI 输出边界：稳定 JSON 结果与可读终端展示。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any


def response(
    command: str,
    data: object,
    *,
    fingerprint: str | None,
    duration_ms: float,
) -> dict[str, Any]:
    """所有单次命令统一输出契约；不解释业务执行含义。"""
    status = "PASS"
    message = "completed"
    code = "OK"
    target: str | None = None
    endpoint: object = None
    parameters: object = {}
    findings: list[object] = []
    if isinstance(data, dict):
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            status = raw_status
        raw_message = data.get("message")
        if isinstance(raw_message, str):
            message = raw_message
        raw_code = data.get("code")
        if isinstance(raw_code, str):
            code = raw_code
        target_raw = data.get("device_id", data.get("target"))
        if target_raw is not None:
            target = str(target_raw)
        endpoint = data.get("endpoint")
        parameters = data.get("protocol_options", {})
        if isinstance(data.get("findings"), list):
            findings = data["findings"]
    return {
        "schema_version": 1,
        "command": command,
        "status": status,
        "code": code,
        "message": message,
        "timestamp": datetime.now(UTC).isoformat(),
        "duration_ms": round(max(0.0, duration_ms), 3),
        "config_fingerprint": fingerprint,
        "target": target,
        "endpoint": endpoint,
        "parameters": parameters,
        "data": data,
        "findings": findings,
    }


def emit(result: dict[str, Any], *, format_name: str = "table") -> None:
    """JSON 单对象输出；终端以状态/参数和数据明细优先。"""
    if format_name == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False, default=str))
        return
    print(
        f'{result["status"]} {result["command"]}'
        f' ({result["duration_ms"]} ms)'
    )
    payload = result["data"]
    if isinstance(payload, list):
        if not payload:
            print("(empty)")
            return
        if all(isinstance(row, dict) for row in payload):
            keys = list(payload[0])
            # 大对象或变长字段不强行铺成极宽表格。
            if len(keys) <= 8 and all(all(not isinstance(v, dict | list) for v in row.values()) for row in payload):
                widths = {key: min(48, max(len(key), *(len(str(row.get(key, ""))) for row in payload))) for key in keys}
                print("  ".join(key.ljust(widths[key]) for key in keys))
                for row in payload:
                    print("  ".join(str(row.get(key, ""))[:widths[key]].ljust(widths[key]) for key in keys))
                return
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str))


class Timer:
    def __init__(self) -> None:
        self._started = perf_counter()

    def elapsed_ms(self) -> float:
        return (perf_counter() - self._started) * 1000
