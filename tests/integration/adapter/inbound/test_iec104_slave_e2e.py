"""Integration test — IEC104 slave proxy against a minimal master client.

Starts the real :class:`IEC104SlaveServer` (snapshot + handlers + bridge with
a mocked Dispatcher), connects a hand-rolled IEC104 master client over TCP,
and verifies the STARTDT handshake, a batched general interrogation, and a
remote-control command round trip (``ACT_CON`` → ``ACT_TERM``).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.adapter.inbound.iec104_slave import (
    DataSnapshot,
    IEC104SlaveHandlers,
    IEC104SlaveServer,
    SchedulerBridge,
    build_data_type_mapping,
    build_ioa_mapping,
    build_reverse_mapping,
)
from wind_hub.adapter.outbound.protocol.iec104.codec import (
    ASDU,
    CauseOfTransmission,
    IFrame,
    InterrogationCommand,
    SingleCommand,
    TypeID,
    UFrameType,
    decode_apdu,
    decode_asdu,
    encode_asdu,
    encode_i_frame,
    encode_u_frame,
)
from wind_hub.config.schema import ReportingPoint
from wind_hub.domain.model.command import CommandResult
from wind_hub.domain.model.point import PointValue


def _reporting() -> list[ReportingPoint]:
    return [
        ReportingPoint(device_id="wtg-001", point_id="rotor.speed", ioa=101, data_type="M_ME_NC_1"),
        ReportingPoint(device_id="wtg-001", point_id="gen.power", ioa=102, data_type="M_ME_NC_1"),
        ReportingPoint(device_id="wtg-001", point_id="wind.speed", ioa=103, data_type="M_ME_NC_1"),
        ReportingPoint(
            device_id="wtg-001", point_id="status.running", ioa=201, data_type="M_SP_NA_1"
        ),
    ]


class _MasterClient:
    """A minimal IEC104 master: STARTDT handshake, interrogation, command."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._send_seq = 0
        self._recv_seq = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self._port)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    async def _send_i(self, asdu: ASDU) -> None:
        assert self._writer is not None
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) & 0x7FFF
        self._writer.write(encode_i_frame(seq, self._recv_seq, encode_asdu(asdu)))
        await self._writer.drain()

    async def _send_u(self, frame_type: UFrameType) -> None:
        assert self._writer is not None
        self._writer.write(encode_u_frame(frame_type))
        await self._writer.drain()

    async def _read_frame(self) -> bytes | None:
        assert self._reader is not None
        try:
            header = await self._reader.readexactly(2)
        except asyncio.IncompleteReadError:
            return None
        rest = await self._reader.readexactly(header[1])
        return header + rest

    async def _read_asdus(self, stop: tuple[TypeID, CauseOfTransmission]) -> list[ASDU]:
        asdus: list[ASDU] = []
        while True:
            frame_bytes = await self._read_frame()
            assert frame_bytes is not None
            frame = decode_apdu(frame_bytes)
            if not isinstance(frame, IFrame):
                continue
            self._recv_seq = (frame.send_seq + 1) & 0x7FFF
            asdu, _ = decode_asdu(frame.asdu)
            asdus.append(asdu)
            if asdu.type_id == stop[0] and asdu.cause == stop[1]:
                return asdus

    async def startdt(self) -> None:
        await self._send_u(UFrameType.STARTDT_ACT)
        while True:
            frame = decode_apdu(await self._read_frame())  # type: ignore[arg-type]
            if getattr(frame, "frame_type", None) == UFrameType.STARTDT_CON:
                return

    async def interrogate(self) -> list[ASDU]:
        await self._send_i(
            ASDU(
                type_id=TypeID.C_IC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[InterrogationCommand(ioa=0)],
            )
        )
        return await self._read_asdus(
            (TypeID.C_IC_NA_1, CauseOfTransmission.ACTIVATION_TERMINATION)
        )

    async def send_single_command(self, ioa: int, value: bool) -> list[ASDU]:
        await self._send_i(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=ioa, value=value)],
            )
        )
        return await self._read_asdus(
            (TypeID.C_SC_NA_1, CauseOfTransmission.ACTIVATION_TERMINATION)
        )


@pytest.fixture
async def proxy():
    """A started slave proxy with a populated snapshot and mocked dispatcher."""
    reporting = _reporting()
    snapshot = DataSnapshot()
    dispatcher = MagicMock()
    dispatcher.send = AsyncMock(
        side_effect=lambda cmd: CommandResult(command_id=cmd.command_id, success=True)
    )
    bridge = SchedulerBridge(
        scheduler=MagicMock(),
        dispatcher=dispatcher,
        snapshot=snapshot,
        mapping=build_ioa_mapping(reporting),
    )
    handlers = IEC104SlaveHandlers(
        snapshot=snapshot,
        data_type_mapping=build_data_type_mapping(reporting),
        reverse_mapping=build_reverse_mapping(reporting),
        bridge=bridge,
        common_address=1,
        batch_size=2,
    )
    server = IEC104SlaveServer("127.0.0.1", 0, handlers, common_address=1)

    bridge.on_points_collected(
        [
            PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
            PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
            PointValue(device_id="wtg-001", point_id="wind.speed", value=12.5),
            PointValue(device_id="wtg-001", point_id="status.running", value=True),
        ]
    )

    await server.start()
    try:
        yield server, dispatcher
    finally:
        await server.stop()


async def test_full_slave_interrogation_and_command(proxy) -> None:  # type: ignore[no-untyped-def]
    server, dispatcher = proxy
    client = _MasterClient(server.port)
    await client.connect()
    try:
        # 1. STARTDT handshake.
        await client.startdt()

        # 2. General interrogation → ACT_CON + batched data + ACT_TERM.
        asdus = await client.interrogate()

        causes = [a.cause for a in asdus]
        assert causes[0] == CauseOfTransmission.ACTIVATION_CON
        assert causes[-1] == CauseOfTransmission.ACTIVATION_TERMINATION

        # Data ASDUs: M_ME_NC_1 batched 2+1 (batch_size=2), then M_SP_NA_1(1).
        meas_asdus = [a for a in asdus if a.type_id == TypeID.M_ME_NC_1]
        sp_asdus = [a for a in asdus if a.type_id == TypeID.M_SP_NA_1]
        assert [len(a.objects) for a in meas_asdus] == [2, 1]
        meas_objects = [o for a in meas_asdus for o in a.objects]
        assert {o.ioa: o.value for o in meas_objects} == {
            101: 1500.5,
            102: 800.0,
            103: 12.5,
        }
        assert sp_asdus[0].objects[0].ioa == 201
        assert sp_asdus[0].objects[0].value is True

        # 3. Remote command → ACT_CON + ACT_TERM.
        reply = await client.send_single_command(ioa=201, value=True)
        assert [a.cause for a in reply] == [
            CauseOfTransmission.ACTIVATION_CON,
            CauseOfTransmission.ACTIVATION_TERMINATION,
        ]
        sent_cmd = dispatcher.send.await_args.args[0]
        assert sent_cmd.device_id == "wtg-001"
        assert sent_cmd.point_id == "status.running"
        assert sent_cmd.value is True
    finally:
        await client.close()


async def test_proxy_reports_health_and_session_count() -> None:
    snapshot = DataSnapshot()
    handlers = IEC104SlaveHandlers(
        snapshot=snapshot,
        data_type_mapping={},
        reverse_mapping={},
        bridge=SchedulerBridge(
            scheduler=MagicMock(),
            dispatcher=MagicMock(),
            snapshot=snapshot,
            mapping={},
        ),
        common_address=1,
        batch_size=50,
    )
    server = IEC104SlaveServer("127.0.0.1", 0, handlers, common_address=1)
    assert server.health().healthy is False

    await server.start()
    try:
        assert server.health().healthy is True
        assert server.session_count == 0
        client = _MasterClient(server.port)
        await client.connect()
        await asyncio.sleep(0.05)  # let the server register the session
        assert server.session_count == 1
        await client.close()
    finally:
        await server.stop()

    assert server.health().healthy is False
