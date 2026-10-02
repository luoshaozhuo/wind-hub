"""IEC104 从站代理的 ASDU 请求处理器。

支持两类入站请求：
- C_IC_NA_1 站总召：ACT_CON → 按 TypeID 分组的数据 ASDU → ACT_TERM；
- C_SC_NA_1/C_DC_NA_1/C_SE_NC_1 遥控：IOA 映射到设备点位，经 SlaveBridge
  转交 CommandDispatcher，成功返回 ACT_CON+ACT_TERM，失败返回 negative ACT_CON。

单 ASDU 严格受 253-byte APDU 上限约束，batch_size 只是对象数量软上限。
本模块不持有 TCP socket；发送能力由 SlaveSession Protocol 注入。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol
from uuid import uuid4

from wind_hub.adapter.inbound.iec104_slave.bridge import SlaveBridge
from wind_hub.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub_core.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    DoublePoint,
    DoublePointWithTime,
    InterrogationCommand,
    MeasuredValueNormalized,
    MeasuredValueScaled,
    MeasuredValueShort,
    MeasuredValueShortWithTime,
    QualityFlag,
    SinglePoint,
    SinglePointWithTime,
    TypeID,
    from_datetime,
)
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointValue, Quality

# APDU 长度字段单字节，上限 253；ASDU 头 6 字节（TypeID 1 + VSQ 1 +
# COT 1 + OA 1 + CA 2），因此单个 ASDU 的信息对象净荷最多 247 字节。
MAX_APDU_ASDU_BYTES = 253
ASDU_HEADER_BYTES = 6
MAX_ASDU_PAYLOAD_BYTES = MAX_APDU_ASDU_BYTES - ASDU_HEADER_BYTES

# 各监视方向类型的单对象线路上字节数（IOA 3B + 值 + 品质 + 时标）。
OBJECT_SIZE_BYTES: dict[str, int] = {
    "M_SP_NA_1": 4,  # SIQ 1
    "M_DP_NA_1": 4,  # DIQ 1
    "M_ME_NA_1": 6,  # NVA 2 + QDS 1
    "M_ME_NB_1": 6,  # SVA 2 + QDS 1
    "M_ME_NC_1": 8,  # R32 4 + QDS 1
    "M_SP_TB_1": 11,  # SIQ 1 + CP56Time2a 7
    "M_DP_TB_1": 11,  # DIQ 1 + CP56Time2a 7
    "M_ME_TF_1": 15,  # R32 4 + QDS 1 + CP56Time2a 7
}


class SlaveSession(Protocol):
    """Handler 所需的最小 session 发送接口。

    使用 Protocol 避免请求处理逻辑依赖具体 TCP session 实现。
    """

    async def send_asdu(self, asdu: ASDU, negative: bool = False) -> None: ...


def _quality_flag(q: Quality) -> QualityFlag:
    """把 Wind Hub Quality 映射为 IEC104 QualityFlag。"""
    if q == Quality.BAD:
        return QualityFlag.IV
    if q == Quality.UNCERTAIN:
        return QualityFlag.NT
    return QualityFlag(0)


def _build_monitor_object(data_type: str, ioa: int, pv: PointValue) -> Any:
    """按 reporting TypeID 构造监视方向 information object。

    Args:
        data_type: reporting 配置中的 IEC104 TypeID 名称。
        ioa: 目标 IOA。
        pv: 最新 PointValue。

    Returns:
        与 TypeID 对应的强类型 information object。

    Raises:
        ProtocolError: data_type 不在已验证白名单。
    """
    q = _quality_flag(pv.quality)
    if data_type == "M_SP_NA_1":
        return SinglePoint(ioa=ioa, value=bool(pv.value), quality=q)
    if data_type == "M_SP_TB_1":
        return SinglePointWithTime(
            ioa=ioa,
            value=bool(pv.value),
            quality=q,
            timestamp=from_datetime(pv.timestamp),
        )
    if data_type == "M_DP_NA_1":
        return DoublePoint(ioa=ioa, value=int(pv.value), quality=q)
    if data_type == "M_DP_TB_1":
        return DoublePointWithTime(
            ioa=ioa,
            value=int(pv.value),
            quality=q,
            timestamp=from_datetime(pv.timestamp),
        )
    if data_type == "M_ME_NA_1":
        return MeasuredValueNormalized(ioa=ioa, value=float(pv.value), quality=q)
    if data_type == "M_ME_NB_1":
        return MeasuredValueScaled(ioa=ioa, value=int(pv.value), quality=q)
    if data_type == "M_ME_NC_1":
        return MeasuredValueShort(ioa=ioa, value=float(pv.value), quality=q)
    if data_type == "M_ME_TF_1":
        return MeasuredValueShortWithTime(
            ioa=ioa,
            value=float(pv.value),
            quality=q,
            timestamp=from_datetime(pv.timestamp),
        )
    # ReportingConfig 已做白名单校验；到达这里说明配置验证边界被绕过。
    raise ProtocolError(f"Unsupported reporting data_type '{data_type}'")


def _chunk_by_apdu_limit(
    items: list[tuple[int, PointValue]],
    data_type: str,
    batch_size: int,
) -> Iterator[list[tuple[int, PointValue]]]:
    """按 APDU 硬上限和 batch_size 软上限切分同 TypeID 数据。

    Args:
        items: 同一 TypeID 的 (ioa, PointValue) 列表。
        data_type: TypeID 名称，用于获取单对象 wire 大小。
        batch_size: 单批对象数量软上限。

    Yields:
        每个都能装入单个 APDU 的对象批次。
    """
    object_bytes = OBJECT_SIZE_BYTES[data_type]
    chunk: list[tuple[int, PointValue]] = []
    for item in items:
        if chunk and (
            len(chunk) >= batch_size or (len(chunk) + 1) * object_bytes > MAX_ASDU_PAYLOAD_BYTES
        ):
            yield chunk
            chunk = []
        chunk.append(item)
    if chunk:
        yield chunk


class IEC104SlaveHandlers:
    """所有从站 session 共享的请求处理器。

    Handler 不保存每连接状态；数据来自共享 DataSnapshot，命令通过 SlaveBridge
    转发，因此可被多个 session 复用。
    """

    def __init__(
        self,
        snapshot: DataSnapshot,
        data_type_mapping: dict[int, str],
        reverse_mapping: dict[int, tuple[str, str]],
        bridge: SlaveBridge,
        common_address: int,
        batch_size: int,
    ) -> None:
        self._snapshot = snapshot
        self._data_type_mapping = data_type_mapping
        self._reverse_mapping = reverse_mapping
        self._bridge = bridge
        self._common_address = common_address
        self._batch_size = batch_size

    async def handle_interrogation(self, asdu: ASDU, session: SlaveSession) -> None:
        """响应站总召：ACT_CON → 数据 → ACT_TERM。

        Args:
            asdu: 主站总召 ASDU；当前统一按站总召返回完整 snapshot。
            session: 用于发送响应的连接接口。

        Notes:
            snapshot 先按 TypeID 分组，再按 APDU wire 大小切批。
        """
        del asdu  # 当前代理只实现站总召语义，统一返回完整 snapshot。

        await self._send_interrogation_confirmation(session, CauseOfTransmission.ACTIVATION_CON)

        grouped: dict[str, list[tuple[int, PointValue]]] = {}
        for ioa, pv in self._snapshot.get_all():
            dtype = self._data_type_mapping.get(ioa)
            if dtype is None:
                continue
            grouped.setdefault(dtype, []).append((ioa, pv))

        for dtype, items in grouped.items():
            type_id = TypeID.__members__[dtype]
            for chunk in _chunk_by_apdu_limit(items, dtype, self._batch_size):
                objects = [_build_monitor_object(dtype, ioa, pv) for ioa, pv in chunk]
                await session.send_asdu(
                    ASDU(
                        type_id=type_id,
                        cause=CauseOfTransmission.INTERROGATED_BY_STATION,
                        common_address=self._common_address,
                        objects=objects,
                    )
                )

        await self._send_interrogation_confirmation(
            session, CauseOfTransmission.ACTIVATION_TERMINATION
        )

    async def handle_command(self, asdu: ASDU, session: SlaveSession) -> None:
        """转发遥控并根据执行结果返回协议确认。

        Args:
            asdu: 遥控激活 ASDU。
            session: 用于发送确认的连接接口。

        Notes:
            未知 IOA 或 CommandResult.success=False 返回 negative ACT_CON；
            成功返回 positive ACT_CON 后再返回 ACT_TERM。
        """
        for obj in asdu.objects:
            ioa = getattr(obj, "ioa", None)
            point = self._reverse_mapping.get(ioa) if isinstance(ioa, int) else None
            if point is None:
                await self._send_command_confirmation(asdu, [obj], session, ok=False)
                continue

            device_id, point_id = point
            cmd = Command(
                command_id=uuid4().hex,
                device_id=device_id,
                point_id=point_id,
                value=getattr(obj, "value", None),
            )
            result = await self._bridge.forward_command(cmd)
            if result.success:
                await session.send_asdu(
                    ASDU(
                        type_id=asdu.type_id,
                        cause=CauseOfTransmission.ACTIVATION_CON,
                        common_address=self._common_address,
                        objects=[obj],
                    )
                )
                await session.send_asdu(
                    ASDU(
                        type_id=asdu.type_id,
                        cause=CauseOfTransmission.ACTIVATION_TERMINATION,
                        common_address=self._common_address,
                        objects=[obj],
                    )
                )
            else:
                await self._send_command_confirmation(asdu, [obj], session, ok=False)

    # ------------------------------------------------------------------
    # 响应辅助函数
    # ------------------------------------------------------------------

    async def _send_interrogation_confirmation(
        self, session: SlaveSession, cause: CauseOfTransmission
    ) -> None:
        await session.send_asdu(
            ASDU(
                type_id=TypeID.C_IC_NA_1,
                cause=cause,
                common_address=self._common_address,
                objects=[InterrogationCommand(ioa=0)],
            )
        )

    async def _send_command_confirmation(
        self,
        asdu: ASDU,
        objects: list[Any],
        session: SlaveSession,
        ok: bool,
    ) -> None:
        """发送单个正向或否定 ACT_CON。

        Args:
            asdu: 原始请求 ASDU，用于复用 TypeID。
            objects: 需要回显的 information object。
            session: 发送接口。
            ok: True 为正向确认，False 设置 P/N 否定位。
        """
        await session.send_asdu(
            ASDU(
                type_id=asdu.type_id,
                cause=CauseOfTransmission.ACTIVATION_CON,
                common_address=self._common_address,
                objects=objects,
            ),
            negative=not ok,
        )
