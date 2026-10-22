"""DC-EMS 示例机组写点原始地址契约。

来自设计书追溯的 setId=1、slave.reg.address；不擅自做 40001 偏移变换。
高低字节编码仍须用 DC-EMS highBit=1 实际向量验收。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from seahorse.infrastructure.models.wind_turbine.contract import COMMAND_POINTS, READ_POINTS

RegisterArea = Literal["coil", "discrete_input", "input", "holding"]


@dataclass(frozen=True, slots=True)
class OriginalAddress:
    slave: int
    area: RegisterArea
    offset: int
    kind: Literal["F", "I", "B"]


CONTROL_ADDRESSES: dict[str, OriginalAddress] = {
    "ctrlReActPow": OriginalAddress(1, "holding", 41555, "F"),
    "ctrlActPow": OriginalAddress(1, "holding", 41557, "F"),
    "emsActPowerSetInertia": OriginalAddress(1, "holding", 41559, "F"),
    "emsReactPowerSetStatusWrite": OriginalAddress(1, "holding", 41561, "I"),
    "emsActPowerChangeRate": OriginalAddress(1, "holding", 41563, "F"),
    "emsActCtrlMode": OriginalAddress(1, "holding", 41565, "I"),
    "ctrlStart": OriginalAddress(1, "coil", 12808, "B"),
    "ctrlStop": OriginalAddress(1, "coil", 12809, "B"),
    "ctrlReset": OriginalAddress(1, "coil", 12810, "B"),
}

READ_ALIASES: dict[str, str] = {
    "emsReactPowerSet": "ctrlReActPow",
    "emsActPowerSet": "ctrlActPow",
    "emsActPowerRead": "ctrlActPow",
    "emsReactPowerSetStatus": "emsReactPowerSetStatusWrite",
}


def validate_original_addresses(
    addresses: dict[str, OriginalAddress],
) -> None:
    """完整 49 点清单需提供 40 个读取字段和 9 个控制字段。

    同址别名必须保留相同的物理寄存器位置；不可分配虚拟新地址。
    """
    missing = (READ_POINTS | COMMAND_POINTS) - addresses.keys()
    if missing:
        raise ValueError(f"missing DC-EMS mapping: {sorted(missing)}")
    for point, expected in CONTROL_ADDRESSES.items():
        if addresses[point] != expected:
            raise ValueError(f"incorrect control address: {point}")
    for alias, target in READ_ALIASES.items():
        if addresses[alias] != addresses[target]:
            raise ValueError(f"shared register alias mismatch: {alias} -> {target}")
    # 对同一个物理位置，允许且仅允许上述同址别名。
    by_location: dict[tuple[int, str, int], str] = {}
    for point, address in sorted(addresses.items()):
        key = (address.slave, address.area, address.offset)
        previous = by_location.get(key)
        canonical = READ_ALIASES.get(point, point)
        if previous is not None and previous != canonical:
            raise ValueError(f"unexpected register alias at {key}: {point}")
        by_location[key] = canonical
