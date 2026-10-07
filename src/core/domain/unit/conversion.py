"""工程单位换算。"""

from __future__ import annotations

from .model import Unit


def convert_value(value: float, source: Unit, target: Unit) -> float:
    """在同一工程量类别的单位之间换算数值。

    Args:
        value: 源单位下的数值。
        source: 源单位。
        target: 目标单位。

    Returns:
        目标单位下的数值。

    Raises:
        ValueError: source 与 target 属于不同 Quantity。
    """
    if source.quantity != target.quantity:
        raise ValueError(
            f"incompatible unit quantities: {source.quantity} -> {target.quantity}"
        )

    base_value = value * source.scale_to_base + source.offset_to_base
    return (base_value - target.offset_to_base) / target.scale_to_base
