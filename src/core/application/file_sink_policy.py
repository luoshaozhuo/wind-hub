"""CSV FileSink 的纯配置与轮转决策；不执行文件 I/O。

Adapter 对实际写入进行串行化，并在每次追加整个 batch 前调用决策函数。
大小限制按 UTF-8 字节数计算，且 batch 不得拆分为不完整的 CSV 行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CSV_COLUMNS = ("timestamp", "device_id", "point_id", "value", "quality", "source")
MIB = 1024 * 1024


class RotationMode(StrEnum):
    NONE = "none"
    SIZE = "size"
    DAILY = "daily"
    SIZE_OR_DAILY = "size_or_daily"


class FileSinkPolicy(BaseModel):
    """FileSink 新契约：目录、容量、时间轮转和有界保留。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    rotation: RotationMode = RotationMode.SIZE_OR_DAILY
    max_size_mb: float = Field(default=100.0, gt=0)
    max_files: int = Field(default=30, ge=1)
    buffer_size: int = Field(default=100, ge=1)
    flush_interval: float = Field(default=1.0, ge=0)
    fsync_on_flush: bool = False
    write_header: bool = True

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        if Path(value).suffix.lower() not in ("", ".csv"):
            raise ValueError("path must be a directory or .csv file")
        return value

    @model_validator(mode="after")
    def validate_rotation(self) -> FileSinkPolicy:
        if self.rotation == RotationMode.NONE:
            raise ValueError("unbounded file rotation is not permitted")
        return self

    @property
    def max_size_bytes(self) -> int:
        return max(1, int(self.max_size_mb * MIB))


@dataclass(frozen=True, slots=True)
class FileSegment:
    """文件轮转状态，由 Adapter 在独占写锁下维护。"""

    opened_at: datetime
    byte_size: int
    has_records: bool

    def __post_init__(self) -> None:
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("opened_at must be timezone-aware")
        if self.byte_size < 0:
            raise ValueError("byte_size must be nonnegative")


class RotationReason(StrEnum):
    NONE = "none"
    SIZE = "size"
    DAILY = "daily"


def rotation_reason(
    policy: FileSinkPolicy,
    segment: FileSegment,
    *,
    pending_bytes: int,
    now: datetime,
) -> RotationReason:
    """计算追加前是否轮转。

    单条记录超过上限时允许单个新文件超限，不允许空文件重复轮转。
    已包含 CSV 表头的 pending_bytes 应由调用方精确计算。
    """
    if pending_bytes < 0:
        raise ValueError("pending_bytes must be nonnegative")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    mode = policy.rotation
    if mode in (RotationMode.DAILY, RotationMode.SIZE_OR_DAILY):
        if now.astimezone(UTC).date() != segment.opened_at.astimezone(UTC).date():
            if segment.has_records:
                return RotationReason.DAILY
    if mode in (RotationMode.SIZE, RotationMode.SIZE_OR_DAILY):
        if segment.has_records and segment.byte_size + pending_bytes > policy.max_size_bytes:
            return RotationReason.SIZE
    return RotationReason.NONE


def expired_segments(
    paths: list[Path],
    *,
    max_files: int,
) -> tuple[Path, ...]:
    """按调用方提供的创建顺序（旧到新）选出超出保留数量的文件。

    调用方只允许传入当前 Sink 拥有的归档段，不得扫描并删除其他文件；
    不包含正在写入的活动文件。max_files 表示保留的归档文件数量。
    """
    if max_files < 1:
        raise ValueError("max_files must be >= 1")
    return tuple(paths[: max(0, len(paths) - max_files)])
