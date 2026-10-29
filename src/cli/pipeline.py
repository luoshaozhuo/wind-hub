"""只读自动诊断流水线：按配置逐设备检查，首次失败立即停止。"""

from __future__ import annotations

import asyncio

from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.infrastructure.config.effective import resolve_effective_device

from .protocol_diagnostic import create_driver, sample_dict


async def run_pipeline(
    snapshot: ConfigSnapshot,
    *,
    start_device: str | None = None,
) -> dict[str, Any]:
    """设备连接及点位读取顺序稳定；不得执行写入或污染生产会话。"""
    steps: list[dict[str, Any]] = []
    device_ids = sorted(str(key) for key in snapshot.site.devices)
    if start_device is not None:
        if start_device not in device_ids:
            raise ValueError(f"resume device no longer configured: {start_device}")
        device_ids = device_ids[device_ids.index(start_device):]

    for device_id in device_ids:
        device = next(
            item for key, item in snapshot.site.devices.items() if str(key) == device_id
        )
        if not device.enabled:
            steps.append({"device": device_id, "status": "SKIPPED", "reason": "disabled"})
            continue
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        resolved, options = resolve_effective_device(device, model, table.protocol.name)
        context = {
            "device": device_id,
            "protocol": table.protocol.name,
            "endpoint": f"{resolved.endpoint.host}:{resolved.endpoint.port}",
            "protocol_options": {
                key: "***" if any(
                    part in key.lower() for part in ("password", "token", "secret", "key")
                ) else value
                for key, value in options.items()
            },
        }
        driver = create_driver(snapshot, device_id)
        failure: str | None = None
        try:
            async with asyncio.timeout(5.0):
                await driver.connect()
            steps.append({**context, "operation": "connect", "status": "PASS"})
            for point in sorted(table.points.values(), key=lambda item: item.point_id):
                from core.domain import PointAccess

                if point.access not in (PointAccess.READ, PointAccess.READ_WRITE):
                    continue
                async with asyncio.timeout(5.0):
                    sample = await driver.read_one(point.point_id)
                steps.append({
                    **context,
                    "operation": "read",
                    "point": point.point_id,
                    "status": "PASS" if sample.quality.value == "good" else "WARNING",
                    "sample": sample_dict(sample),
                })
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                async with asyncio.timeout(5.0):
                    await driver.close()
            except Exception as exc:
                if failure is None:
                    failure = f"close failed: {type(exc).__name__}: {exc}"
        if failure is not None:
            steps.append({**context, "status": "FAIL", "message": failure})
            return {"status": "FAIL", "failed_device": device_id, "steps": steps}
    return {"status": "PASS", "failed_device": None, "steps": steps}
