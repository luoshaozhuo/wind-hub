"""IEC104 server/session/handler 单元测试。

验证 resolved Sink 点经 ExportedSinkPointValue 进入快照后，总召分组、APDU
切分、品质映射和控制方向否定确认保持正确。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from wind_hub_collector.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub_collector.adapter.inbound.iec104_slave.handlers import (
    MAX_APDU_ASDU_BYTES,
    MAX_ASDU_PAYLOAD_BYTES,
    OBJECT_SIZE_BYTES,
    IEC104SlaveHandlers,
)
from wind_hub_collector.adapter.inbound.iec104_slave.session import IEC104SlaveSession
from wind_hub_collector.application.sink_export import SinkReferenceExporter
from wind_hub_core.config.sinks import IEC104SinkAddress, ResolvedSinkPoint, SinkSource
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    SingleCommand,
    TypeID,
    encode_asdu,
)


def _definition(
    point_id: str,
    ioa: int,
    type_id: str,
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wtg-001", point_id=point_id),
        ref=f"wtg-001.{point_id}",
        source_data_type="float32",
        source_unit="none",
        datatype="float32",
        unit="none",
        address=IEC104SinkAddress(ioa=ioa, type_id=type_id),  # type: ignore[arg-type]
    )


def _definitions() -> list[ResolvedSinkPoint]:
    return [
        _definition("rotor.speed", 1001, "M_ME_NC_1"),
        _definition("gen.power", 1002, "M_ME_NC_1"),
        _definition("status.running", 2001, "M_SP_NA_1"),
    ]


def _snapshot(
    definitions: list[ResolvedSinkPoint],
    values: list[PointValue],
) -> DataSnapshot:
    snap = DataSnapshot()
    snap.update(SinkReferenceExporter(definitions).export(values))
    return snap


class _RecordingSession:
    def __init__(self) -> None:
        self.sent: list[tuple[ASDU, bool]] = []

    async def send_asdu(self, asdu: ASDU, negative: bool = False) -> None:
        self.sent.append((asdu, negative))


def _make_handlers(snapshot: DataSnapshot, batch_size: int = 50) -> IEC104SlaveHandlers:
    return IEC104SlaveHandlers(
        snapshot=snapshot,
        common_address=1,
        batch_size=batch_size,
    )


def _bulk_setup(
    n: int, type_id: str, ioa_base: int = 1001
) -> DataSnapshot:
    definitions = [
        _definition(f"p{ioa_base + i}", ioa_base + i, type_id)
        for i in range(n)
    ]
    values = [
        PointValue(device_id="wtg-001", point_id=d.source.point_id, value=1.0)
        for d in definitions
    ]
    return _snapshot(definitions, values)


def _interrogation() -> ASDU:
    return ASDU(
        type_id=TypeID.C_IC_NA_1,
        cause=CauseOfTransmission.ACTIVATION,
        common_address=1,
    )


class TestDataSnapshot:
    def test_update_keeps_exported_ioa(self) -> None:
        definitions = [_definition("rotor.speed", 1001, "M_ME_NC_1")]
        snap = _snapshot(
            definitions,
            [PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5)],
        )
        assert snap.size == 1
        assert snap.get(1001) is not None
        assert snap.get(1001).value == 1500.5  # type: ignore[union-attr]

    def test_get_all_sorted_by_ioa(self) -> None:
        definitions = [
            _definition("b", 300, "M_ME_NC_1"),
            _definition("a", 100, "M_ME_NC_1"),
            _definition("c", 200, "M_ME_NC_1"),
        ]
        snap = _snapshot(
            definitions,
            [
                PointValue(device_id="wtg-001", point_id="a", value=1),
                PointValue(device_id="wtg-001", point_id="b", value=2),
                PointValue(device_id="wtg-001", point_id="c", value=3),
            ],
        )
        assert [ioa for ioa, _ in snap.get_all()] == [100, 200, 300]


class TestInterrogation:
    async def test_interrogation_emits_con_data_term_grouped_by_type(self) -> None:
        snap = _snapshot(
            _definitions(),
            [
                PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
                PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
                PointValue(device_id="wtg-001", point_id="status.running", value=True),
            ],
        )
        handlers = _make_handlers(snap)
        rec = _RecordingSession()
        await handlers.handle_interrogation(_interrogation(), rec)

        assert [a.type_id for a, _ in rec.sent] == [
            TypeID.C_IC_NA_1,
            TypeID.M_ME_NC_1,
            TypeID.M_SP_NA_1,
            TypeID.C_IC_NA_1,
        ]
        assert rec.sent[0][0].cause == CauseOfTransmission.ACTIVATION_CON
        assert rec.sent[-1][0].cause == CauseOfTransmission.ACTIVATION_TERMINATION
        assert [o.ioa for o in rec.sent[1][0].objects] == [1001, 1002]
        assert [o.value for o in rec.sent[1][0].objects] == [1500.5, 800.0]
        assert rec.sent[2][0].objects[0].value is True

    async def test_interrogation_batches_by_batch_size(self) -> None:
        snap = _snapshot(
            _definitions()[:2],
            [
                PointValue(device_id="wtg-001", point_id="rotor.speed", value=1.0),
                PointValue(device_id="wtg-001", point_id="gen.power", value=2.0),
            ],
        )
        rec = _RecordingSession()
        await _make_handlers(snap, batch_size=1).handle_interrogation(_interrogation(), rec)
        data = [a for a, _ in rec.sent if a.type_id == TypeID.M_ME_NC_1]
        assert [len(a.objects) for a in data] == [1, 1]

    async def test_interrogation_chunks_short_floats_by_apdu_bytes(self) -> None:
        snap = _bulk_setup(500, "M_ME_NC_1")
        rec = _RecordingSession()
        await _make_handlers(snap, batch_size=500).handle_interrogation(_interrogation(), rec)
        data = [a for a, _ in rec.sent if a.type_id == TypeID.M_ME_NC_1]
        assert sum(len(a.objects) for a in data) == 500
        for asdu in data:
            assert len(asdu.objects) * OBJECT_SIZE_BYTES["M_ME_NC_1"] <= MAX_ASDU_PAYLOAD_BYTES
            assert len(encode_asdu(asdu)) <= MAX_APDU_ASDU_BYTES
        assert len(data) >= 17

    async def test_interrogation_chunks_single_points_by_apdu_bytes(self) -> None:
        snap = _bulk_setup(500, "M_SP_NA_1")
        rec = _RecordingSession()
        await _make_handlers(snap, batch_size=500).handle_interrogation(_interrogation(), rec)
        data = [a for a, _ in rec.sent if a.type_id == TypeID.M_SP_NA_1]
        assert sum(len(a.objects) for a in data) == 500
        for asdu in data:
            assert len(asdu.objects) * OBJECT_SIZE_BYTES["M_SP_NA_1"] <= MAX_ASDU_PAYLOAD_BYTES
            assert len(encode_asdu(asdu)) <= MAX_APDU_ASDU_BYTES
        assert len(data) == 9


class _FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass


class TestSession:
    async def test_command_dispatch_is_rejected_without_device_write(self) -> None:
        handlers = MagicMock()
        session = IEC104SlaveSession(
            reader=MagicMock(),
            writer=_FakeWriter(),  # type: ignore[arg-type]
            handlers=handlers,
            common_address=1,
        )
        await session._dispatch(  # noqa: SLF001
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=2001, value=True)],
            )
        )
        handlers.handle_command.assert_not_called()
        data = bytes(session._writer.buf)  # noqa: SLF001
        assert data[8] & 0x80

    async def test_send_asdu_negative_sets_pn_bit(self) -> None:
        session = IEC104SlaveSession(
            reader=MagicMock(),
            writer=_FakeWriter(),  # type: ignore[arg-type]
            handlers=MagicMock(),
            common_address=1,
        )
        await session.send_asdu(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION_CON,
                common_address=1,
                objects=[SingleCommand(ioa=2001, value=True)],
            ),
            negative=True,
        )
        data = bytes(session._writer.buf)  # noqa: SLF001
        assert data[0] == 0x68
        assert data[8] & 0x80
