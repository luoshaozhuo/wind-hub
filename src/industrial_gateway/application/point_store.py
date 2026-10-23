"""点位存储：协议无关、写入提交后发布命令事件。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from time import monotonic

from industrial_gateway.domain import (
    PointDefinition,
    PointKey,
    PointRole,
    PointSnapshot,
    PointWrite,
    Scalar,
)

CommandHandler = Callable[[PointWrite], Awaitable[None]]


class PointStore:
    """单事件循环下的共享点位存储；同一逻辑点写入严格串行。"""

    def __init__(
        self,
        definitions: Iterable[PointDefinition],
        *,
        command_handler: CommandHandler | None = None,
        max_age_seconds: Mapping[PointKey, float] | None = None,
    ) -> None:
        self._definitions: dict[PointKey, PointDefinition] = {}
        for definition in definitions:
            key = (definition.device_id, definition.point_id)
            if key in self._definitions:
                raise ValueError(f"duplicate point: {key}")
            self._definitions[key] = definition
        self._values: dict[PointKey, PointSnapshot] = {}
        self._updated_at: dict[PointKey, float] = {}
        self._max_age_seconds = dict(max_age_seconds or {})
        for key, seconds in self._max_age_seconds.items():
            if key not in self._definitions or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f"invalid feedback max age: {key}")
        self._locks = {key: asyncio.Lock() for key in self._definitions}
        self._command_handler = command_handler

    def read(self, device_id: str, point_id: str) -> PointSnapshot:
        key = (device_id, point_id)
        self._definition(key)
        snapshot = self._values.get(key)
        if snapshot is None:
            return PointSnapshot(device_id, point_id, None, 0)
        max_age = self._max_age_seconds.get(key)
        if max_age is not None and monotonic() - self._updated_at[key] > max_age:
            return PointSnapshot(device_id, point_id, None, snapshot.version)
        return snapshot

    def is_uninitialized_command(self, device_id: str, point_id: str) -> bool:
        """命令点未收到真实下发时，仅供 Modbus 读取层选择初始镜像。"""
        key = (device_id, point_id)
        return self._definition(key).role is PointRole.COMMAND and key not in self._values

    async def write(self, request: PointWrite) -> PointSnapshot:
        """校验并提交逻辑点位；命令须由执行端接受后才提交内存镜像。

        Handler 必须是具有背压和拒绝机制的命令接收端，不代表现场执行完成。
        未配置 Handler 的命令点一律拒绝，避免虚假的控制成功。
        """
        key = (request.device_id, request.point_id)
        definition = self._definition(key)
        if not definition.writable:
            raise PermissionError(f"point is not writable: {key}")
        self._validate_scalar(request.value)
        self._validate_data_type(definition.data_type, request.value)
        if not request.source:
            raise ValueError("write source must not be empty")
        async with self._locks[key]:
            if definition.role is PointRole.COMMAND:
                if self._command_handler is None:
                    raise RuntimeError("command handler is not configured")
                await self._command_handler(request)
            return self._replace(key, request.value)

    async def update_feedback(self, device_id: str, point_id: str, value: Scalar) -> PointSnapshot:
        """内部反馈刷新不产生命令事件，也不得覆盖命令/参数点。"""
        key = (device_id, point_id)
        definition = self._definition(key)
        if definition.role is not PointRole.FEEDBACK:
            raise PermissionError(f"not a feedback point: {key}")
        self._validate_scalar(value)
        self._validate_data_type(definition.data_type, value)
        async with self._locks[key]:
            return self._replace(key, value)

    async def update_batch(self, values: Mapping[PointKey, Scalar], *, source: str) -> None:
        """Collector 数据批次：整体校验后一次提交，不产生控制事件。"""
        if not source:
            raise ValueError("source must not be empty")
        for key, value in values.items():
            definition = self._definition(key)
            if definition.role is not PointRole.FEEDBACK:
                raise PermissionError(f"not a feedback point: {key}")
            self._validate_scalar(value)
            self._validate_data_type(definition.data_type, value)
        # 本方法不包含 await；同一事件循环内的读操作不会看见半个批次。
        for key, value in values.items():
            self._replace(key, value)

    async def invalidate_feedback(self, device_id: str, point_id: str) -> None:
        """失效时清除可读镜像，防止服务端继续返回过期数据。"""
        key = (device_id, point_id)
        if self._definition(key).role is not PointRole.FEEDBACK:
            raise PermissionError(f"not a feedback point: {key}")
        async with self._locks[key]:
            self._values.pop(key, None)
            self._updated_at.pop(key, None)

    def _replace(self, key: PointKey, value: Scalar) -> PointSnapshot:
        previous = self._values.get(key)
        version = 1 if previous is None else previous.version + 1
        result = PointSnapshot(key[0], key[1], value, version)
        self._values[key] = result
        self._updated_at[key] = monotonic()
        return result

    def _definition(self, key: PointKey) -> PointDefinition:
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise KeyError(f"unknown point: {key}") from exc

    @staticmethod
    def _validate_scalar(value: Scalar) -> None:
        if not isinstance(value, bool | int | float):
            raise TypeError("point value must be bool, int or float")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite point value")

    @staticmethod
    def _validate_data_type(data_type: str | None, value: Scalar) -> None:
        """入库前按 Modbus 声明类型校验，杜绝成功写入后无法编码。"""
        if data_type is None:
            return
        if data_type == "bool":
            if type(value) is not bool:
                raise TypeError("bool point requires bool value")
            return
        integers = {
            "int16": (-2**15, 2**15 - 1),
            "uint16": (0, 2**16 - 1),
            "int32": (-2**31, 2**31 - 1),
            "uint32": (0, 2**32 - 1),
            "int64": (-2**63, 2**63 - 1),
            "uint64": (0, 2**64 - 1),
        }
        if data_type in integers:
            lower, upper = integers[data_type]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{data_type} point requires in-range integer")
        elif data_type in {"float32", "float64"}:
            if type(value) not in {int, float}:
                raise TypeError("float point requires numeric value")
            if data_type == "float32" and abs(value) > 3.4028234663852886e38:
                raise ValueError("float32 overflow")
