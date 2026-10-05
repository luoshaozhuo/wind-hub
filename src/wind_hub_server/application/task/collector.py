"""Task 控制面共用的 Collector 访问辅助。

TaskControlService 与 TaskPlacementReconciler 共用同一份 Collector 身份校验，
避免 placement safety 相关语义出现第二份实现。
"""

from __future__ import annotations

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import CollectorPort


class TaskWorkerUnavailableError(RuntimeError):
    """Task 所属 Collector 当前不可用或身份非法。"""


async def verified_collector(
    collectors: CollectorDirectory,
    worker_id: str,
) -> CollectorPort:
    """返回在线且身份与 placement worker_id 一致的 Collector。"""
    collector = collectors.get(worker_id)
    try:
        status = await collector.config_status()
    except Exception as exc:
        raise TaskWorkerUnavailableError(
            f"collector '{worker_id}' is unavailable"
        ) from exc
    reported_id = str(status.get("collector_id") or "")
    if reported_id != worker_id:
        raise TaskWorkerUnavailableError(
            f"collector identity mismatch: expected={worker_id} "
            f"reported={reported_id or '<empty>'}"
        )
    return collector


__all__ = ["TaskWorkerUnavailableError", "verified_collector"]
