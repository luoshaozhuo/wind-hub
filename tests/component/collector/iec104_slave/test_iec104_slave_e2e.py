"""IEC104 Sink TCP component test。"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.iec104.codec import (
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(port: int) -> ResolvedSinkConfig:
    points = []
    specs = [
        ("rotor.speed", 101, "M_ME_NC_1"),
        ("gen.power", 102, "M_ME_NC_1"),
        ("wind.speed", 103, "M_ME_NC_1"),
        ("status.running", 201, "M_SP_NA_1"),
    ]
    for point_id, ioa, type_id in specs:
        points.append(
            {
                "source": {"device_id": "wtg-001", "point_id": point_id},
                "ref": f"wtg-001.{point_id}",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {"ioa": ioa, "type_id": type_id},
            }
        )
    return ResolvedSinkConfig(
        name="scada",
        type="iec104",
        connection={
            "host": "127.0.0.1",
            "port": port,
            "common_address": 1,
            "batch_size": 2,
        },
        points=points,
    )


class _MasterClient:
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
            raw = await self._read_frame()
            assert raw is not None
            frame = decode_apdu(raw)
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

    async def send_single_command(self, ioa: int, value: bool) -> tuple[ASDU, bool]:
        await self._send_i(
            ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=1,
                objects=[SingleCommand(ioa=ioa, value=value)],
            )
        )
        while True:
            raw = await self._read_frame()
            assert raw is not None
            frame = decode_apdu(raw)
            if not isinstance(frame, IFrame):
                continue
            self._recv_seq = (frame.send_seq + 1) & 0x7FFF
            asdu, _ = decode_asdu(frame.asdu)
            return asdu, bool(frame.asdu[2] & 0x80)


@pytest.fixture
async def sink():
    port = _free_port()
    iec104 = IEC104Sink(_config(port))
    await iec104.write(
        [
            PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
            PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
            PointValue(device_id="wtg-001", point_id="wind.speed", value=12.5),
            PointValue(device_id="wtg-001", point_id="status.running", value=True),
        ]
    )
    await iec104.open()
    try:
        yield iec104
    finally:
        await iec104.close()


async def test_full_sink_interrogation_and_reject_command(sink) -> None:  # type: ignore[no-untyped-def]
    client = _MasterClient(sink.port)
    await client.connect()
    try:
        await client.startdt()
        asdus = await client.interrogate()
        assert asdus[0].cause == CauseOfTransmission.ACTIVATION_CON
        assert asdus[-1].cause == CauseOfTransmission.ACTIVATION_TERMINATION
        meas = [a for a in asdus if a.type_id == TypeID.M_ME_NC_1]
        sp = [a for a in asdus if a.type_id == TypeID.M_SP_NA_1]
        assert [len(a.objects) for a in meas] == [2, 1]
        values = {o.ioa: o.value for a in meas for o in a.objects}
        assert values == {101: 1500.5, 102: 800.0, 103: 12.5}
        assert sp[0].objects[0].ioa == 201
        reply, negative = await client.send_single_command(201, True)
        assert reply.type_id == TypeID.C_SC_NA_1
        assert reply.cause == CauseOfTransmission.ACTIVATION_CON
        assert negative is True
    finally:
        await client.close()
