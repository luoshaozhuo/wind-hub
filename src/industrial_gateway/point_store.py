"""点位存储：协议无关、写入提交后发布命令事件。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable

from .domain import PointDefinition, PointRole, PointSnapshot, PointWrite, Scalar

CommandHandler = Callable[[PointWrite], Awaitable[None]]
PointKey = tuple[str, str]


class PointStore:
    """单事件循环下的共享点位存储；同一逻辑点写入严格串行。"""

    def __init__(
        self,
        definitions: Iterable[PointDefinition],
        *,
        command_handler: CommandHandler | None = None,
    ) -> None:
        self._definitions: dict[PointKey, PointDefinition] = {}
        for definition in definitions:
            key = (definition.device_id, definition.point_id)
            if key in self._definitions:
                raise ValueError(f"duplicate point: {key}")
            self._definitions[key] = definition
        self._values: dict[PointKey, PointSnapshot] = {}
        self._locks = {key: asyncio.Lock() for key in self._definitions}
        self._command_handler = command_handler

    def read(self, device_id: str, point_id: str) -> PointSnapshot:
        key = (device_id, point_id)
        self._definition(key)
        return self._values.get(key, PointSnapshot(device_id, point_id, None, 0))

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
        if not request.source:
            raise ValueError("write source must not be empty")
        async with self._locks[key]:
            if definition.role is PointRole.COMMAND:
                if self._command_handler is None:
                    raise RuntimeError("command handler is not configured")
                await self._command_handler(request)
            return self._replace(key, request.value)

    async def update_feedback(
        self, device_id: str, point_id: str, value: Scalar
    ) -> PointSnapshot:
        """内部反馈刷新不产生命令事件，也不得覆盖命令/参数点。"""
        key = (device_id, point_id)
        if self._definition(key).role is not PointRole.FEEDBACK:
            raise PermissionError(f"not a feedback point: {key}")
        self._validate_scalar(value)
        async with self._locks[key]:
            return self._replace(key, value)

    def _replace(self, key: PointKey, value: Scalar) -> PointSnapshot:
        previous = self._values.get(key)
        result = PointSnapshot(key[0], key[1], value, 1 if previous is None else previous.version + 1)
        self._values[key] = result
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
