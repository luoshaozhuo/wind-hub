"""Reporting-config → lookup-table compilation.

Turns the declarative ``reporting`` list into O(1) dictionaries consumed by
the snapshot (point identity → IOA) and the interrogation/command handlers
(IOA → data type and IOA → point identity).

Pure logic — no I/O, no asyncio.
"""

from __future__ import annotations

from wind_hub.config.schema import ReportingPoint


def build_ioa_mapping(reporting: list[ReportingPoint]) -> dict[tuple[str, str], int]:
    """Map ``(device_id, point_id)`` → IOA for every reported point."""
    return {(p.device_id, p.point_id): p.ioa for p in reporting}


def build_data_type_mapping(reporting: list[ReportingPoint]) -> dict[int, str]:
    """Map IOA → monitor-direction ASDU type string (e.g. ``'M_ME_NC_1'``)."""
    return {p.ioa: p.data_type for p in reporting}


def build_reverse_mapping(reporting: list[ReportingPoint]) -> dict[int, tuple[str, str]]:
    """Map IOA → ``(device_id, point_id)`` for command addressing."""
    return {p.ioa: (p.device_id, p.point_id) for p in reporting}
