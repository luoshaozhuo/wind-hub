"""协议无关的采样标量与质量枚举，供 Domain/Application 共享。"""

from __future__ import annotations

from enum import StrEnum
from typing import TypeAlias

PointScalar: TypeAlias = float | int | bool | str | None
WritableScalar: TypeAlias = float | int | bool | str


class Quality(StrEnum):
    """统一点值质量语义。"""

    GOOD = "good"
    BAD = "bad"
    UNCERTAIN = "uncertain"
