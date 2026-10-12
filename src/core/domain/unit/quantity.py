"""工程量类别。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Quantity:
    """工程量身份值对象。

    这里表达业务量类别，而不是纯物理量纲。例如有功、无功和视在功率
    具有相同物理量纲，但在业务语义上不可互换，因此分别建模。

    具体 Quantity 集合由配置（units.yaml）定义；Domain 只固定身份
    语义（字符串 ID 值对象），不枚举全部工程量。
    """

    quantity_id: str

    def __post_init__(self) -> None:
        quantity_id = self.quantity_id.strip()
        if not quantity_id or quantity_id != self.quantity_id:
            raise ValueError("quantity_id must be a non-empty trimmed string")

    def __str__(self) -> str:
        return self.quantity_id


#: 无量纲是 Domain 校验固有语义（bool/string 点必须无量纲），
#: 与配置定义的具体 Quantity 集合无关，因此保留为代码常量。
DIMENSIONLESS = Quantity("dimensionless")
