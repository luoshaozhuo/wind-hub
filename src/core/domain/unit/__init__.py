"""Shared Domain 工程单位。

Unit / Quantity 是不可变值对象；单位集合由配置（units.yaml）定义并
经 UnitCatalog 随 ConfigSnapshot 传递，不在代码中枚举。
"""

from .catalog import UnitCatalog
from .conversion import convert_value
from .model import Unit
from .quantity import DIMENSIONLESS, Quantity

__all__ = [
    "DIMENSIONLESS",
    "Quantity",
    "Unit",
    "UnitCatalog",
    "convert_value",
]
