"""协议无关的逻辑测点与写入请求契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

Scalar: TypeAlias = bool | int | float


class PointRole(StrEnum):
    FEEDBACK = "feedback"
    PARAMETER = "parameter"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class PointDefinition:
    device_id: str
    point_id: str
    role: PointRole
    writable: bool = False

    def __post_init__(self) -> None:
        if not self.device_id or not self.point_id:
            raise ValueError("device_id and point_id must not be empty")
        if self.role is PointRole.COMMAND and not self.writable:
            raise ValueError("command points must be writable")


@dataclass(frozen=True, slots=True)
class PointWrite:
    device_id: str
    point_id: str
    value: Scalar
    source: str


@dataclass(frozen=True, slots=True)
class PointSnapshot:
    device_id: str
    point_id: str
    value: Scalar | None
    version: int
