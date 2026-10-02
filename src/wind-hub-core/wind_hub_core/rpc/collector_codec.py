"""Collector 强类型 Protobuf 到 Python 控制面 DTO 的纯转换。

本模块只处理 wire message，不依赖 Collector/Server/CLI Runtime；用于 Server 和
wind-hub-ctl 共享同一套字段语义。
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from wind_hub_core.rpc import collector_pb2 as pb


def _optional_wrapper(message: Any, field: str) -> Any:
    """读取 wrapper 字段；未设置时返回 None。"""
    return getattr(message, field).value if message.HasField(field) else None


def task_summary_to_dict(message: pb.TaskSummaryMessage) -> dict[str, Any]:
    """TaskSummaryMessage → JSON 兼容字典。"""
    return {
        "task_id": message.task_id,
        "device": _optional_wrapper(message, "device"),
        "device_group": _optional_wrapper(message, "device_group"),
        "point_group": message.point_group,
        "interval": _optional_wrapper(message, "interval"),
        "targets": list(message.targets),
        "enabled": message.enabled,
        "runtime_state": message.runtime_state,
        "instance_count": message.instance_count,
        "running_instances": message.running_instances,
        "stopped_instances": message.stopped_instances,
        "failed_instances": message.failed_instances,
    }


def task_instance_to_dict(message: pb.TaskInstanceMessage) -> dict[str, Any]:
    """TaskInstanceMessage → JSON 兼容字典。"""
    return {
        "instance_id": message.instance_id,
        "task_id": message.task_id,
        "device_id": message.device_id,
        "point_group": message.point_group,
        "interval": _optional_wrapper(message, "interval"),
        "targets": list(message.targets),
        "state": message.state,
    }


def config_diff_to_dict(message: pb.ConfigDiffMessage) -> dict[str, Any]:
    """ConfigDiffMessage → 既有 ReloadResult diff 字典。"""
    def section(item: Any) -> dict[str, list[str]]:
        return {
            "added": list(item.added),
            "removed": list(item.removed),
            "updated": list(item.updated),
            "unchanged": list(item.unchanged),
        }

    return {
        "devices": section(message.devices),
        "sinks": section(message.sinks),
        "tasks": section(message.tasks),
        "points_changed": message.points_changed,
        "point_tables_changed": list(message.point_tables_changed),
        "units_changed": message.units_changed,
    }


def runtime_status_to_dict(message: pb.RuntimeStatusResponse) -> dict[str, Any]:
    """RuntimeStatusResponse → JSON 兼容字典。"""
    return {
        "running": message.running,
        "device_count": message.device_count,
        "sink_count": message.sink_count,
        "devices_connected": message.devices_connected,
        "sinks_healthy": message.sinks_healthy,
        "points_collected": message.points_collected,
        "points_routed": message.points_routed,
        "points_dropped": message.points_dropped,
        "acquisitions": [
            {
                "instance_id": item.instance_id,
                "task_id": item.task_id,
                "device_id": item.device_id,
                "point_group": item.point_group,
                "running": item.running,
                "consecutive_failures": item.consecutive_failures,
                "last_error": item.last_error or None,
                "last_duration": _optional_wrapper(item, "last_duration"),
            }
            for item in message.acquisitions
        ],
    }


def metrics_snapshot_to_dict(message: pb.MetricsSnapshotResponse) -> dict[str, Any]:
    """MetricsSnapshotResponse → 现有 metrics snapshot 字典。"""
    counters = message.counters
    return {
        "counters": {
            "points_total": counters.points_total,
            "points_bad": counters.points_bad,
            "acquisition_runs": counters.acquisition_runs,
            "acquisition_failures": counters.acquisition_failures,
            "acquisition_partial": counters.acquisition_partial,
            "missed_cycles": counters.missed_cycles,
            "poll_overruns": counters.poll_overruns,
            "connect_failures": counters.connect_failures,
            "reconnects": counters.reconnects,
        },
        "device_connect_failures": {
            item.key: item.value for item in message.device_connect_failures
        },
        "device_reconnects": {
            item.key: item.value for item in message.device_reconnects
        },
        "events": [
            {
                "timestamp": item.timestamp.ToDatetime(tzinfo=UTC).isoformat(),
                "kind": item.kind,
                "object": item.object,
                "message": item.message,
            }
            for item in message.events
        ],
    }


def device_info_to_dict(message: pb.DeviceInfoMessage) -> dict[str, Any]:
    """DeviceInfoMessage → JSON 兼容字典。"""
    return {
        "device_id": message.device_id,
        "protocol": message.protocol,
        "connected": message.connected,
        "last_seen": (
            message.last_seen.ToDatetime(tzinfo=UTC).isoformat()
            if message.HasField("last_seen")
            else None
        ),
        "consecutive_failures": message.consecutive_failures,
        "last_error": message.last_error or None,
    }


def sink_info_to_dict(message: pb.SinkInfoMessage) -> dict[str, Any]:
    """SinkInfoMessage → JSON 兼容字典。"""
    return {
        "name": message.name,
        "healthy": message.healthy,
        "message": message.message or None,
        "queue_depth": message.queue_depth,
    }


def collector_info_to_dict(message: pb.CollectorInfoResponse) -> dict[str, Any]:
    """CollectorInfoResponse → JSON 兼容字典。"""
    return {
        "component": message.component,
        "collector_id": message.collector_id,
        "boot_id": message.boot_id,
        "config_hash": message.config_hash,
        "active_config_hash": message.active_config_hash,
        "prepared_config_hash": message.prepared_config_hash or None,
        "boot_config_hash": message.boot_config_hash,
        "config_revision": message.config_revision or None,
        "active_revision": message.active_revision,
        "prepared_revision": message.prepared_revision or None,
        "runtime_running": message.runtime_running,
    }
