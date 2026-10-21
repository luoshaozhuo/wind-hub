"""协议无关的写入分发：按来源权限和点位角色决定执行路径。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain import PointRole, PointWrite, Scalar

PointKey = tuple[str, str]


class WriterRole(StrEnum):
    DATA = "data"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class ResolvedWrite:
    """协议层必须先完成地址解码和来源认证，再创建此请求。"""

    values: Mapping[PointKey, Scalar]
    writer_role: WriterRole


class WriteService:
    """一个请求允许多个完整数据点，命令请求只允许一个完整命令点。"""

    def __init__(
        self,
        store: PointStore,
        point_roles: Mapping[PointKey, PointRole],
    ) -> None:
        self._store = store
        self._roles = dict(point_roles)

    async def write(self, request: ResolvedWrite) -> None:
        if not request.values:
            raise ValueError("empty write request")
        try:
            roles = {self._roles[key] for key in request.values}
        except KeyError as exc:
            raise ValueError(f"unmapped write point: {exc}") from exc
        if request.writer_role is WriterRole.DATA:
            if roles != {PointRole.FEEDBACK}:
                raise PermissionError("data writers can only update feedback")
            await self._store.update_batch(request.values, source="collector")
            return
        if roles != {PointRole.COMMAND} or len(request.values) != 1:
            raise PermissionError("command request must contain exactly one command point")
        (device_id, point_id), value = next(iter(request.values.items()))
        await self._store.write(PointWrite(device_id, point_id, value, "dc-ems"))
