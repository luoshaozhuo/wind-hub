"""Unit tests for IEC104 session internals.

Tests aspects of the session that can be exercised without a real
TCP connection — helpers, point mapping, ASDU processing, frame
handlers, and close() safety.
"""

from __future__ import annotations

import asyncio

import pytest

from wind_hub.adapter.outbound.protocol.iec104.codec.apci import SFrame
from wind_hub.adapter.outbound.protocol.iec104.codec.asdu import ASDU
from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    DoublePoint,
    MeasuredValueShort,
    SinglePoint,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
    UFrameType,
)
from wind_hub.adapter.outbound.protocol.iec104.connection import ConnectionState
from wind_hub.adapter.outbound.protocol.iec104.session import (
    IEC104Session,
    _extract_quality,
    _extract_value,
)
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointValue, Quality

# ---------------------------------------------------------------------------
# Session fixture — created but not started (no TCP)
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> IEC104Session:
    return IEC104Session(
        host="127.0.0.1",
        port=2404,
        common_addr=1,
        k=12,
        w=8,
        t1=0.1,
        t2=0.1,
        t3=0.1,
    )


# ===========================================================================
# helpers — _extract_value / _extract_quality
# ===========================================================================


class TestExtractValue:
    def test_single_point_value(self) -> None:
        sp = SinglePoint(ioa=100, value=True, quality=QualityFlag(0))
        assert _extract_value(sp) is True

    def test_single_point_false(self) -> None:
        sp = SinglePoint(ioa=100, value=False, quality=QualityFlag(0))
        assert _extract_value(sp) is False

    def test_double_point(self) -> None:
        dp = DoublePoint(ioa=200, value=2, quality=QualityFlag(0))
        assert _extract_value(dp) == 2

    def test_measured_value_short(self) -> None:
        mv = MeasuredValueShort(ioa=300, value=3.14, quality=QualityFlag(0))
        assert _extract_value(mv) == 3.14

    def test_unknown_object_returns_none(self) -> None:
        class Unknown:
            pass

        assert _extract_value(Unknown()) is None


class TestExtractQuality:
    def test_good_quality(self) -> None:
        sp = SinglePoint(ioa=1, value=True, quality=QualityFlag(0))
        assert _extract_quality(sp) == Quality.GOOD

    def test_invalid_bit(self) -> None:
        sp = SinglePoint(ioa=1, value=True, quality=QualityFlag(0) | QualityFlag.IV)
        assert _extract_quality(sp) == Quality.BAD

    def test_not_topical(self) -> None:
        sp = SinglePoint(ioa=1, value=True, quality=QualityFlag(0) | QualityFlag.NT)
        assert _extract_quality(sp) == Quality.UNCERTAIN

    def test_substituted(self) -> None:
        sp = SinglePoint(ioa=1, value=True, quality=QualityFlag(0) | QualityFlag.SB)
        assert _extract_quality(sp) == Quality.UNCERTAIN

    def test_blocked(self) -> None:
        sp = SinglePoint(ioa=1, value=True, quality=QualityFlag(0) | QualityFlag.BL)
        assert _extract_quality(sp) == Quality.UNCERTAIN

    def test_overflow(self) -> None:
        sp = SinglePoint(ioa=1, value=True, quality=QualityFlag(0) | QualityFlag.OV)
        assert _extract_quality(sp) == Quality.UNCERTAIN

    def test_no_quality_attribute(self) -> None:
        class NoQuality:
            pass

        assert _extract_quality(NoQuality()) == Quality.GOOD

    def test_non_qualityflag_attribute(self) -> None:
        class BadQuality:
            quality = "nope"

        assert _extract_quality(BadQuality()) == Quality.GOOD


# ===========================================================================
# point mapping
# ===========================================================================


class TestPointMapping:
    def test_initial_mapping_is_empty(self, session: IEC104Session) -> None:
        assert session.get_ioa("rotor.speed") is None
        assert session.get_point_id(100) is None

    def test_set_and_lookup(self, session: IEC104Session) -> None:
        session.set_points_mapping(
            {100: "rotor.speed", 200: "gen.power"},
            {"rotor.speed": 100, "gen.power": 200},
        )
        assert session.get_ioa("rotor.speed") == 100
        assert session.get_ioa("gen.power") == 200
        assert session.get_point_id(100) == "rotor.speed"
        assert session.get_point_id(200) == "gen.power"

    def test_get_ioa_missing(self, session: IEC104Session) -> None:
        session.set_points_mapping({100: "rotor.speed"}, {"rotor.speed": 100})
        assert session.get_ioa("unknown") is None

    def test_get_point_id_missing(self, session: IEC104Session) -> None:
        session.set_points_mapping({100: "rotor.speed"}, {"rotor.speed": 100})
        assert session.get_point_id(999) is None

    def test_set_points_mapping_makes_copy(self, session: IEC104Session) -> None:
        ioa_map = {100: "rotor.speed"}
        pid_map = {"rotor.speed": 100}
        session.set_points_mapping(ioa_map, pid_map)

        # Mutate the originals.
        ioa_map[200] = "gen.power"
        pid_map["gen.power"] = 200

        # Session should not see the mutations.
        assert session.get_ioa("gen.power") is None
        assert session.get_point_id(200) is None


# ===========================================================================
# properties
# ===========================================================================


class TestProperties:
    def test_initial_state(self, session: IEC104Session) -> None:
        assert session.state == ConnectionState.DISCONNECTED

    def test_is_started_initially_false(self, session: IEC104Session) -> None:
        assert not session.is_started

    def test_interrogation_complete_initially_true(self, session: IEC104Session) -> None:
        assert session.interrogation_complete

    def test_point_cache_initially_empty(self, session: IEC104Session) -> None:
        assert session.point_cache == {}

    def test_point_cache_is_copy(self, session: IEC104Session) -> None:
        """Point cache property returns a copy, not a reference."""
        session.set_points_mapping({100: "x"}, {"x": 100})
        cache = session.point_cache
        cache[100] = PointValue(
            device_id="d",
            point_id="x",
            value=42,
        )
        assert session.point_cache == {}  # original untouched


# ===========================================================================
# close() safety
# ===========================================================================


class TestClose:
    async def test_close_safe_when_not_started(self, session: IEC104Session) -> None:
        """close() should be safe even when never started."""
        await session.close()

    async def test_close_idempotent(self, session: IEC104Session) -> None:
        """close() twice should not raise."""
        await session.close()
        await session.close()

    async def test_start_raises_after_close(self, session: IEC104Session) -> None:
        """start() after close() should raise ProtocolError."""
        await session.close()
        with pytest.raises(ProtocolError, match="closed"):
            await session.start()


# ===========================================================================
# S-frame handler
# ===========================================================================


class TestFrameHandlers:
    async def test_s_frame_updates_ack(self, session: IEC104Session) -> None:
        """S-frame should update the flow controller ack."""
        # Manually advance send sequence.
        for _ in range(3):
            session._flow.on_sent()

        assert session._flow.outstanding == 3

        # Simulate receiving an S-frame with N(R)=3.
        await session._handle_s_frame(SFrame(recv_seq=3))

        assert session._flow.outstanding == 0

    async def test_s_frame_partial_ack(self, session: IEC104Session) -> None:
        """S-frame with N(R)=1 acknowledges first frame, not second."""
        for _ in range(2):
            session._flow.on_sent()

        await session._handle_s_frame(SFrame(recv_seq=1))
        assert session._flow.outstanding == 1

    async def test_s_frame_clears_ack_is_outstanding(
        self,
        session: IEC104Session,
    ) -> None:
        session._flow.on_sent()
        assert session._flow.ack_is_outstanding()

        await session._handle_s_frame(SFrame(recv_seq=1))
        assert not session._flow.ack_is_outstanding()


# ===========================================================================
# ASDU processing — cache_objects
# ===========================================================================


class TestCacheObjects:
    async def test_cache_updates_with_mapped_ioa(
        self,
        session: IEC104Session,
    ) -> None:
        session.set_points_mapping(
            {100: "rotor.speed", 200: "gen.power"},
            {"rotor.speed": 100, "gen.power": 200},
        )
        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.INTERROGATED_BY_STATION,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=1500.5, quality=QualityFlag(0)),
                MeasuredValueShort(ioa=200, value=50.0, quality=QualityFlag(0)),
            ],
        )
        await session._cache_objects(asdu)

        cache = session.point_cache
        assert 100 in cache
        assert 200 in cache
        assert cache[100].point_id == "rotor.speed"
        assert cache[100].value == 1500.5
        assert cache[200].point_id == "gen.power"
        assert cache[200].value == 50.0

    async def test_cache_ignores_unmapped_ioa(
        self,
        session: IEC104Session,
    ) -> None:
        session.set_points_mapping(
            {100: "rotor.speed"},
            {"rotor.speed": 100},
        )
        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.INTERROGATED_BY_STATION,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=42.0, quality=QualityFlag(0)),
                MeasuredValueShort(ioa=999, value=99.0, quality=QualityFlag(0)),
            ],
        )
        await session._cache_objects(asdu)

        cache = session.point_cache
        assert 100 in cache  # mapped
        assert 999 not in cache  # unmapped

    async def test_cache_overwrites_existing_ioa(
        self,
        session: IEC104Session,
    ) -> None:
        session.set_points_mapping(
            {100: "rotor.speed"},
            {"rotor.speed": 100},
        )
        asdu1 = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.INTERROGATED_BY_STATION,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=10.0, quality=QualityFlag(0)),
            ],
        )
        await session._cache_objects(asdu1)
        assert session.point_cache[100].value == 10.0

        # Update.
        asdu2 = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.PERIODIC,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=20.0, quality=QualityFlag(0)),
            ],
        )
        await session._cache_objects(asdu2)
        assert session.point_cache[100].value == 20.0

    async def test_on_value_update_callback(
        self,
        session: IEC104Session,
    ) -> None:
        called_values: list[PointValue] = []

        def cb(pv: PointValue) -> None:
            called_values.append(pv)

        session.set_on_value_update(cb)
        session.set_points_mapping(
            {100: "rotor.speed"},
            {"rotor.speed": 100},
        )
        asdu = ASDU(
            type_id=TypeID.M_ME_NC_1,
            cause=CauseOfTransmission.INTERROGATED_BY_STATION,
            common_address=1,
            objects=[
                MeasuredValueShort(ioa=100, value=123.45, quality=QualityFlag(0)),
            ],
        )
        await session._cache_objects(asdu)

        assert len(called_values) == 1
        assert called_values[0].point_id == "rotor.speed"
        assert called_values[0].value == 123.45


# ===========================================================================
# U-frame handler
# ===========================================================================


class TestUFrameHandler:
    async def test_startdt_con_fires_event(self, session: IEC104Session) -> None:
        """Receiving STARTDT_CON should set the startdt_event."""
        session._startdt_event = asyncio.Event()

        from wind_hub.adapter.outbound.protocol.iec104.codec.apci import UFrame

        await session._handle_u_frame(UFrame(frame_type=UFrameType.STARTDT_CON))
        assert session._startdt_event.is_set()

    async def test_testfr_act_replies_with_con(self, session: IEC104Session) -> None:
        """Receiving TESTFR_ACT should enqueue TESTFR_CON."""
        from wind_hub.adapter.outbound.protocol.iec104.codec.apci import UFrame

        await session._handle_u_frame(UFrame(frame_type=UFrameType.TESTFR_ACT))
        # TESTFR_CON should be enqueued.
        assert session._send_queue.qsize() >= 1

    async def test_stopdt_act_replies_with_con(self, session: IEC104Session) -> None:
        """Receiving STOPDT_ACT should enqueue STOPDT_CON."""
        from wind_hub.adapter.outbound.protocol.iec104.codec.apci import UFrame

        await session._handle_u_frame(UFrame(frame_type=UFrameType.STOPDT_ACT))
        assert session._send_queue.qsize() >= 1


# ===========================================================================
# I-frame handler — flow control
# ===========================================================================


class TestIFrameHandler:
    async def test_i_frame_advances_recv_seq(self, session: IEC104Session) -> None:
        """Receiving an I-frame should advance the receive sequence."""
        from wind_hub.adapter.outbound.protocol.iec104.codec.apci import IFrame

        assert session._flow.recv_seq_for_ack() == 0
        await session._handle_i_frame(
            IFrame(send_seq=0, recv_seq=0, asdu=b"\x01\x01\x14\x00\x01\x00")
        )
        assert session._flow.recv_seq_for_ack() == 1


# ===========================================================================
# _process_asdu — interrogation lifecycle
# ===========================================================================


class TestProcessASDU:
    async def test_activation_termination_sets_event(
        self,
        session: IEC104Session,
    ) -> None:
        """ACTIVATION_TERMINATION on C_IC_NA_1 sets the done event."""
        session._interrogation_done.clear()
        asdu = ASDU(
            type_id=TypeID.C_IC_NA_1,
            cause=CauseOfTransmission.ACTIVATION_TERMINATION,
            common_address=1,
            objects=[],
        )
        await session._process_asdu(asdu)
        assert session._interrogation_done.is_set()

    async def test_activation_con_noops(self, session: IEC104Session) -> None:
        """ACTIVATION_CON on C_IC_NA_1 does nothing special."""
        asdu = ASDU(
            type_id=TypeID.C_IC_NA_1,
            cause=CauseOfTransmission.ACTIVATION_CON,
            common_address=1,
            objects=[],
        )
        # Should not raise.
        await session._process_asdu(asdu)
