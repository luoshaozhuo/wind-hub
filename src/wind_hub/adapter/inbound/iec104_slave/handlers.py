"""ASDU-level request handling for the IEC104 slave proxy.

Two inbound request families are handled:

* **general interrogation** (``C_IC_NA_1`` activation) — acknowledged with
  ``ACT_CON``, answered with the full snapshot in data batches (grouped by
  ASDU type, since a single ASDU carries one type; each batch bounded by
  the 253-byte APDU limit, with ``batch_size`` as a soft object-count cap),
  then closed with ``ACT_TERM``;
* **remote control commands** (``C_SC_NA_1`` / ``C_DC_NA_1`` / ``C_SE_NC_1``
  activation) — resolved IOA → point, forwarded through the bridge to the
  :class:`Dispatcher`, and answered ``ACT_CON`` + ``ACT_TERM`` on success or a
  single negative ``ACT_CON`` on failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol
from uuid import uuid4

from wind_hub.adapter.inbound.iec104_slave.bridge import SchedulerBridge
from wind_hub.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub.adapter.outbound.protocol.iec104.codec import (
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
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointValue, Quality

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
    """The slice of :class:`IEC104SlaveSession` handlers depend on."""

    async def send_asdu(self, asdu: ASDU, negative: bool = False) -> None: ...


def _quality_flag(q: Quality) -> QualityFlag:
    """Map an engine :class:`Quality` onto an IEC104 :class:`QualityFlag`."""
    if q == Quality.BAD:
        return QualityFlag.IV
    if q == Quality.UNCERTAIN:
        return QualityFlag.NT
    return QualityFlag(0)


def _build_monitor_object(data_type: str, ioa: int, pv: PointValue) -> Any:
    """Build the monitor-direction info object for *pv* under *data_type*.

    The value is coerced to the field type the ASDU type expects; the
    reporting point's ``data_type`` is already validated against a whitelist.
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
    # Unreachable — data_type is whitelisted by ReportingConfig validation.
    raise ProtocolError(f"Unsupported reporting data_type '{data_type}'")


def _chunk_by_apdu_limit(
    items: list[tuple[int, PointValue]],
    data_type: str,
    batch_size: int,
) -> Iterator[list[tuple[int, PointValue]]]:
    """Split one type group into chunks that fit a single APDU.

    The hard bound is the 253-byte APDU limit: accumulated object bytes
    must stay within :data:`MAX_ASDU_PAYLOAD_BYTES`.  ``batch_size``
    survives as a soft object-count cap so small-object types (e.g.
    M_SP_NA_1 at 4B, 61 per APDU) cannot explode the ASDU count.  Every
    whitelisted type is far smaller than the payload bound, so a chunk
    always holds at least one object.
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
    """Request handlers shared by every slave session (stateless over snapshot)."""

    def __init__(
        self,
        snapshot: DataSnapshot,
        data_type_mapping: dict[int, str],
        reverse_mapping: dict[int, tuple[str, str]],
        bridge: SchedulerBridge,
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
        """Answer a general interrogation: ``ACT_CON`` → data → ``ACT_TERM``.

        The snapshot is grouped by ASDU type (an ASDU carries a single type),
        then each group is chunked by :func:`_chunk_by_apdu_limit` — the
        253-byte APDU limit is the hard bound, ``batch_size`` the soft
        object-count cap.
        """
        del asdu  # QOI is ignored — every interrogation serves the full snapshot.

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
        """Forward each command object to the Dispatcher and echo the outcome.

        Success → positive ``ACT_CON`` + ``ACT_TERM``; failure (unknown IOA or
        a non-successful dispatch) → a single negative ``ACT_CON``.
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
    # helpers
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
        """Emit a positive or negative single confirmation for *objects*."""
        await session.send_asdu(
            ASDU(
                type_id=asdu.type_id,
                cause=CauseOfTransmission.ACTIVATION_CON,
                common_address=self._common_address,
                objects=objects,
            ),
            negative=not ok,
        )
