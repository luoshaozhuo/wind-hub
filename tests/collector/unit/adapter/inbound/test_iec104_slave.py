"""Unit tests for the IEC104 slave proxy (从站) components.

Covers the pure pieces — snapshot, mapping, handlers, bridge — plus the
session's negative-confirmation encoding, all without a real socket.  The
Scheduler and Dispatcher are replaced by mocks (their real contracts are
exercised by the integration test).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from wind_hub.adapter.inbound.iec104_slave.bridge import SlaveBridge
from wind_hub.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub.adapter.inbound.iec104_slave.handlers import (
    MAX_APDU_ASDU_BYTES,
    MAX_ASDU_PAYLOAD_BYTES,
    OBJECT_SIZE_BYTES,
    IEC104SlaveHandlers,
)
from wind_hub.adapter.inbound.iec104_slave.mapping import (
    build_data_type_mapping,
    build_ioa_mapping,
    build_reverse_mapping,
)
from wind_hub.adapter.inbound.iec104_slave.session import IEC104SlaveSession
from wind_hub.adapter.outbound.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    SingleCommand,
    TypeID,
    encode_asdu,
)
from wind_hub_core.config.schema import ReportingPoint
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.point import PointValue


def _reporting() -> list[ReportingPoint]:
    return [
        ReportingPoint(
            device_id="wtg-001", point_id="rotor.speed", ioa=1001, data_type="M_ME_NC_1"
        ),
        ReportingPoint(device_id="wtg-001", point_id="gen.power", ioa=1002, data_type="M_ME_NC_1"),
        ReportingPoint(
            device_id="wtg-001", point_id="status.running", ioa=2001, data_type="M_SP_NA_1"
        ),
    ]


class _RecordingSession:
    """Fake session that records ``(ASDU, negative)`` send calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[ASDU, bool]] = []

    async def send_asdu(self, asdu: ASDU, negative: bool = False) -> None:
        self.sent.append((asdu, negative))


def _make_handlers(
    snapshot: DataSnapshot,
    dispatcher: MagicMock,
    batch_size: int = 50,
    reporting: list[ReportingPoint] | None = None,
) -> IEC104SlaveHandlers:
    reporting = reporting if reporting is not None else _reporting()
    bridge = SlaveBridge(
        dispatcher=dispatcher,
        snapshot=snapshot,
        mapping=build_ioa_mapping(reporting),
    )
    return IEC104SlaveHandlers(
        snapshot=snapshot,
        data_type_mapping=build_data_type_mapping(reporting),
        reverse_mapping=build_reverse_mapping(reporting),
        bridge=bridge,
        common_address=1,
        batch_size=batch_size,
    )


def _bulk_setup(
    n: int, data_type: str, ioa_base: int = 1001
) -> tuple[list[ReportingPoint], DataSnapshot]:
    """Build *n* reporting points of one data_type plus a filled snapshot."""
    reporting = [
        ReportingPoint(
            device_id="wtg-001",
            point_id=f"p{ioa_base + i}",
            ioa=ioa_base + i,
            data_type=data_type,
        )
        for i in range(n)
    ]
    snapshot = DataSnapshot()
    snapshot.update(
        [PointValue(device_id="wtg-001", point_id=p.point_id, value=1.0) for p in reporting],
        build_ioa_mapping(reporting),
    )
    return reporting, snapshot


def _interrogation() -> ASDU:
    return ASDU(
        type_id=TypeID.C_IC_NA_1,
        cause=CauseOfTransmission.ACTIVATION,
        common_address=1,
    )


# ---------------------------------------------------------------------------
# DataSnapshot
# ---------------------------------------------------------------------------


class TestDataSnapshot:
    def test_update_only_keeps_mapped_points(self) -> None:
        snap = DataSnapshot()
        mapping = {("wtg-001", "rotor.speed"): 1001}
        snap.update(
            [
                PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
                PointValue(device_id="wtg-001", point_id="unmapped", value=1.0),
            ],
            mapping,
        )
        assert snap.size == 1
        assert snap.get(1001).value == 1500.5  # type: ignore[union-attr]
        assert snap.get(9999) is None

    def test_get_all_sorted_by_ioa(self) -> None:
        snap = DataSnapshot()
        mapping = {
            ("wtg-001", "b"): 300,
            ("wtg-001", "a"): 100,
            ("wtg-001", "c"): 200,
        }
        snap.update(
            [
                PointValue(device_id="wtg-001", point_id="a", value=1),
                PointValue(device_id="wtg-001", point_id="b", value=2),
                PointValue(device_id="wtg-001", point_id="c", value=3),
            ],
            mapping,
        )
        assert [ioa for ioa, _ in snap.get_all()] == [100, 200, 300]


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


class TestMapping:
    def test_ioa_mapping(self) -> None:
        m = build_ioa_mapping(_reporting())
        assert m[("wtg-001", "rotor.speed")] == 1001

    def test_data_type_mapping(self) -> None:
        m = build_data_type_mapping(_reporting())
        assert m[2001] == "M_SP_NA_1"

    def test_reverse_mapping(self) -> None:
        m = build_reverse_mapping(_reporting())
        assert m[1002] == ("wtg-001", "gen.power")


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------


class TestBridge:
    async def test_on_points_collected_updates_snapshot(self) -> None:
        snapshot = DataSnapshot()
        bridge = SlaveBridge(
            dispatcher=MagicMock(),
            snapshot=snapshot,
            mapping=build_ioa_mapping(_reporting()),
        )
        bridge.on_points_collected(
            [PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5)]
        )
        assert snapshot.get(1001).value == 1500.5  # type: ignore[union-attr]

    async def test_forward_command_calls_dispatcher(self) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=CommandResult(command_id="c1", success=True))
        bridge = SlaveBridge(
            dispatcher=dispatcher,
            snapshot=DataSnapshot(),
            mapping=build_ioa_mapping(_reporting()),
        )
        cmd = Command(command_id="c1", device_id="wtg-001", point_id="p", value=True)
        result = await bridge.forward_command(cmd)
        assert result.success
        dispatcher.send.assert_awaited_once_with(cmd)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


class TestInterrogation:
    async def test_interrogation_emits_con_data_term_grouped_by_type(self) -> None:
        snapshot = DataSnapshot()
        snapshot.update(
            [
                PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
                PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
                PointValue(device_id="wtg-001", point_id="status.running", value=True),
            ],
            build_ioa_mapping(_reporting()),
        )
        handlers = _make_handlers(snapshot, MagicMock())
        rec = _RecordingSession()

        await handlers.handle_interrogation(
            ASDU(
                type_id=TypeID.C_IC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
            ),
            rec,
        )

        # ACT_CON | M_ME_NC_1(2) | M_SP_NA_1(1) | ACT_TERM
        assert [a.type_id for a, _neg in rec.sent] == [
            TypeID.C_IC_NA_1,
            TypeID.M_ME_NC_1,
            TypeID.M_SP_NA_1,
            TypeID.C_IC_NA_1,
        ]
        assert rec.sent[0][0].cause == CauseOfTransmission.ACTIVATION_CON
        assert rec.sent[3][0].cause == CauseOfTransmission.ACTIVATION_TERMINATION

        meas_asdu = rec.sent[1][0]
        assert meas_asdu.cause == CauseOfTransmission.INTERROGATED_BY_STATION
        assert [o.ioa for o in meas_asdu.objects] == [1001, 1002]
        assert [o.value for o in meas_asdu.objects] == [1500.5, 800.0]

        sp_asdu = rec.sent[2][0]
        assert sp_asdu.objects[0].ioa == 2001
        assert sp_asdu.objects[0].value is True

    async def test_interrogation_batches_by_batch_size(self) -> None:
        snapshot = DataSnapshot()
        snapshot.update(
            [
                PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
                PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
            ],
            build_ioa_mapping(_reporting()),
        )
        handlers = _make_handlers(snapshot, MagicMock(), batch_size=1)
        rec = _RecordingSession()

        await handlers.handle_interrogation(
            ASDU(
                type_id=TypeID.C_IC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
            ),
            rec,
        )

        # two M_ME_NC_1 data ASDUs (one object each), then ACT_TERM
        data_asdus = [a for a, _neg in rec.sent if a.type_id == TypeID.M_ME_NC_1]
        assert len(data_asdus) == 2
        assert [a.objects[0].ioa for a in data_asdus] == [1001, 1002]

    async def test_interrogation_chunks_short_floats_by_apdu_bytes(self) -> None:
        """500 个 M_ME_NC_1（8B/对象）：按 253B APDU 上限切分，不得溢出。

        step23 修复的生产 bug：batch_size=50 时单 ASDU 406B 溢出，
        500 点总召必崩（encode 抛 ValueError）。
        """
        reporting, snapshot = _bulk_setup(500, "M_ME_NC_1")
        # 软上限抬高到不触发， isolating 纯字节切分。
        handlers = _make_handlers(snapshot, MagicMock(), batch_size=500, reporting=reporting)
        rec = _RecordingSession()

        await handlers.handle_interrogation(_interrogation(), rec)

        data_asdus = [a for a, _neg in rec.sent if a.type_id == TypeID.M_ME_NC_1]
        assert sum(len(a.objects) for a in data_asdus) == 500
        for a in data_asdus:
            assert len(a.objects) * OBJECT_SIZE_BYTES["M_ME_NC_1"] <= MAX_ASDU_PAYLOAD_BYTES
            # 真实编码校验：线路上 ASDU 必须装进单字节长度字段。
            assert len(encode_asdu(a)) <= MAX_APDU_ASDU_BYTES
        # 247//8 = 30 → 500 点至少切 17 帧。
        assert len(data_asdus) >= 17

    async def test_interrogation_chunks_single_points_by_apdu_bytes(self) -> None:
        """500 个 M_SP_NA_1（4B/对象）：每帧 <= 61 对象（247//4）。"""
        reporting, snapshot = _bulk_setup(500, "M_SP_NA_1")
        handlers = _make_handlers(snapshot, MagicMock(), batch_size=500, reporting=reporting)
        rec = _RecordingSession()

        await handlers.handle_interrogation(_interrogation(), rec)

        data_asdus = [a for a, _neg in rec.sent if a.type_id == TypeID.M_SP_NA_1]
        assert sum(len(a.objects) for a in data_asdus) == 500
        for a in data_asdus:
            assert len(a.objects) * OBJECT_SIZE_BYTES["M_SP_NA_1"] <= MAX_ASDU_PAYLOAD_BYTES
            assert len(encode_asdu(a)) <= MAX_APDU_ASDU_BYTES
        # 500 = 8×61 + 12 → 恰好 9 帧。
        assert len(data_asdus) == 9

    async def test_interrogation_mixed_types_grouped_and_within_limit(self) -> None:
        """混合类型：按类型分组切分，每帧单类型且不超字节上限。"""
        reporting, snapshot = _bulk_setup(100, "M_ME_NC_1", ioa_base=1001)
        reporting_sp, snapshot_sp = _bulk_setup(100, "M_SP_NA_1", ioa_base=2001)
        reporting = reporting + reporting_sp
        snapshot.update(
            [PointValue(device_id="wtg-001", point_id=p.point_id, value=1) for p in reporting_sp],
            build_ioa_mapping(reporting),
        )
        handlers = _make_handlers(snapshot, MagicMock(), batch_size=500, reporting=reporting)
        rec = _RecordingSession()

        await handlers.handle_interrogation(_interrogation(), rec)

        type_sizes = {"M_ME_NC_1": TypeID.M_ME_NC_1, "M_SP_NA_1": TypeID.M_SP_NA_1}
        for a, _neg in rec.sent:
            if a.type_id in (TypeID.C_IC_NA_1,):
                continue
            dtype = next(k for k, v in type_sizes.items() if v == a.type_id)
            assert len(a.objects) * OBJECT_SIZE_BYTES[dtype] <= MAX_ASDU_PAYLOAD_BYTES
            assert len(encode_asdu(a)) <= MAX_APDU_ASDU_BYTES

    async def test_interrogation_batch_size_soft_cap_applies(self) -> None:
        """batch_size 软上限：字节上限内仍按对象数软上限切分。"""
        # M_SP_NA_1 字节上限允许 61 对象/帧，batch_size=10 应压到 10/帧。
        reporting, snapshot = _bulk_setup(25, "M_SP_NA_1")
        handlers = _make_handlers(snapshot, MagicMock(), batch_size=10, reporting=reporting)
        rec = _RecordingSession()

        await handlers.handle_interrogation(_interrogation(), rec)

        data_asdus = [a for a, _neg in rec.sent if a.type_id == TypeID.M_SP_NA_1]
        assert [len(a.objects) for a in data_asdus] == [10, 10, 5]

    async def test_interrogation_skips_unmapped_ioas(self) -> None:
        snapshot = DataSnapshot()
        # Only populate one mapped point.
        snapshot.update(
            [PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5)],
            build_ioa_mapping(_reporting()),
        )
        handlers = _make_handlers(snapshot, MagicMock())
        rec = _RecordingSession()

        await handlers.handle_interrogation(
            ASDU(
                type_id=TypeID.C_IC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
            ),
            rec,
        )

        assert [a.type_id for a, _neg in rec.sent] == [
            TypeID.C_IC_NA_1,
            TypeID.M_ME_NC_1,
            TypeID.C_IC_NA_1,
        ]


class TestCommand:
    async def test_command_success_emits_con_and_term(self) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=CommandResult(command_id="c1", success=True))
        snapshot = DataSnapshot()
        handlers = _make_handlers(snapshot, dispatcher)
        rec = _RecordingSession()

        await handlers.handle_command(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=2001, value=True)],
            ),
            rec,
        )

        # forward_command received a Command for status.running
        sent_cmd: Command = dispatcher.send.await_args.args[0]
        assert sent_cmd.device_id == "wtg-001"
        assert sent_cmd.point_id == "status.running"
        assert sent_cmd.value is True

        # ACT_CON then ACT_TERM, both positive
        assert [a.cause for a, _neg in rec.sent] == [
            CauseOfTransmission.ACTIVATION_CON,
            CauseOfTransmission.ACTIVATION_TERMINATION,
        ]
        assert all(not neg for _a, neg in rec.sent)

    async def test_command_unknown_ioa_is_negative(self) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(return_value=CommandResult(command_id="c1", success=True))
        snapshot = DataSnapshot()
        handlers = _make_handlers(snapshot, dispatcher)
        rec = _RecordingSession()

        await handlers.handle_command(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=9999, value=True)],
            ),
            rec,
        )

        dispatcher.send.assert_not_awaited()
        assert len(rec.sent) == 1
        asdu, negative = rec.sent[0]
        assert asdu.cause == CauseOfTransmission.ACTIVATION_CON
        assert negative is True

    async def test_command_dispatch_failure_is_negative(self) -> None:
        dispatcher = MagicMock()
        dispatcher.send = AsyncMock(
            return_value=CommandResult(command_id="c1", success=False, error="boom")
        )
        snapshot = DataSnapshot()
        handlers = _make_handlers(snapshot, dispatcher)
        rec = _RecordingSession()

        await handlers.handle_command(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=2001, value=True)],
            ),
            rec,
        )

        assert len(rec.sent) == 1
        assert rec.sent[0][1] is True


# ---------------------------------------------------------------------------
# session — negative confirmation bit
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass


class TestSession:
    async def test_send_asdu_negative_sets_pn_bit(self) -> None:
        session = IEC104SlaveSession(
            reader=MagicMock(),  # unused in send_asdu
            writer=_FakeWriter(),  # type: ignore[arg-type]
            handlers=MagicMock(),  # type: ignore[arg-type]
            common_address=1,
        )
        asdu = ASDU(
            type_id=TypeID.C_SC_NA_1,
            cause=CauseOfTransmission.ACTIVATION_CON,
            common_address=1,
            objects=[SingleCommand(ioa=2001, value=True)],
        )
        await session.send_asdu(asdu, negative=True)

        data = bytes(session._writer.buf)  # noqa: SLF001
        assert data[0] == 0x68
        # COT byte lives at ASDU offset 2 → APDU offset 6 + 2 = 8.
        cot = data[8]
        assert cot & 0x80
        assert cot & 0x3F == CauseOfTransmission.ACTIVATION_CON.value
